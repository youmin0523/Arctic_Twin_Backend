"""
chat_agent.py
=============
북극항로 전용 **의도 기반 오케스트레이션 챗봇** 엔진.

- OpenAI(gpt-4o-mini) function-calling + 스트리밍으로 동작한다.
- 사용자의 자연어 의도를 파악해 보유 AI 모델 도구(RIO·연료/경제성·쇄빙선·출항타이밍·
  빙산 위험·전체 보고서/시나리오)를 **연속 호출**한 뒤, 수치 근거가 있는 단일 결론을
  토큰 단위로 스트리밍 합성한다.
- 도메인 한정: 시스템 프롬프트 + 도메인 한정 도구셋으로 북극항로 외 주제는 거절·유도한다.

설계 원칙(whatif_generator_openai 와 동일):
- 지연 초기화 AsyncOpenAI (OPENAI_API_KEY).
- 도구 스키마/디스패치는 server.py 가 조립해 주입한다(여기선 LLM 루프만 담당, 테스트 용이).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

logger = logging.getLogger("report-service.chat_agent")

CHAT_MODEL = os.environ.get("CHAT_OPENAI_MODEL", "gpt-4o-mini")
MAX_TOOL_TURNS = 8          # 도구 호출 ↔ 결과 피드백 왕복 상한
MAX_HISTORY_MESSAGES = 20   # 세션당 보존 대화(=user/assistant) 메시지 수


SYSTEM_PROMPT = """\
너는 '북극 디지털 트윈 센터'의 북극항로 전문 어시스턴트다. 한국어로, 사람과 대화하듯 자연스럽게 답한다.

[답변 원칙 — 최우선·가장 중요]
- **사용자가 실제로 물은 그 질문에 답하라.** 매번 전체 비용표를 나열하지 마라(앵무새 금지).
  · "12월에 호위 필요해?" → 그 날짜의 위험도·호위 필요 여부를 '직접' 답하라(비용표 X).
  · "지금 NSR 파고·기온 어때?" → 기상만 답하라.
  · "언제 떠나는 게 좋아?" → 출항 적기를 답하라.
  · "총 부대비용·타당성 검토해줘" 처럼 '종합 분석'을 명시할 때만 비용·타당성 전체를 종합하라.
- 답의 길이·구성은 질문에 맞춰라. 초점 질문엔 2~5문장으로 핵심만. 종합 요청엔 구조화된 분석.

[다룰 수 있는 아젠다 — 이 프로젝트(북극 디지털 트윈)가 다루는 영역은 모두 열려 있음]
'북극항로 운항'과 관련해 이 프로젝트가 데이터·모델로 다루는 영역의 질문이면 적극적으로 답하라
(아래는 그 영역의 예시이며, 이 범주에 닿는 질문은 절대 막지 마라):
  · 기상/해상: 날씨·기온·파고·해수온·가시거리·바람·해상상황
  · 빙권: 해빙 농도·두께, 빙산, 빙질, 결빙/해빙 계절
  · 항로: NSR(북동)·NWP(북서)·TSR(횡단극)·대안 SUEZ/CAPE·남극 ROSS/PENINSULA, 거리·관문·통항
  · 안전/위험: POLARIS RIO, Ice Class(PC1~7/IA~IC/Arc4~9), 항행 타당성, 출항 타이밍·계절 적기
  · 쇄빙선: 호위 함대(아라온/CCGS/Rosatom)·호위 필요 여부·비용
  · 경제: 연료·총 부대비용·북극항로 vs 수에즈 경제성
  · 위 항목과 직접 이어지는 북극항로 운항 일반 지식·전망
- 위 프로젝트 도메인을 벗어난 무관한 주제(요리·연예·코딩·정치 등)만 정중히 거절하고 유도하라.
  단, 위 영역에 닿는 질문(특히 기상·해상상황)은 당연히 답하라 — 거절하지 마라.
- 우리 도구·데이터에 없더라도 북극항로 도메인과 닿는 일반 지식·개념·원리·역사·규정·전망 질문이면
  거절하지 말고 너(LLM)의 지식으로 충실히 답하라. 단 ① 실시간 수치(현재 날씨·빙질·빙산·비용 등)는
  반드시 해당 도구로 답하고 추측·환각하지 마라 ② 도구 없이 일반 지식으로 답할 때는 '일반적으로/통상'
  처럼 실시간 실측이 아님을 분명히 밝혀라.

[질문 유형별 도구 선택]
  · 항로 전체 해상기상(파고·해수온·가시거리) → get_route_weather
  · 특정 지역·도시·항구·국가의 현재 날씨(예: 부산, 대한민국, 로테르담) → get_location_weather
  · 특정 날짜의 안전/위험/항행가능/호위필요 → score_route(그 departure_date로) [+ 호위·비용 수치가 필요하면 compare_economics]
  · 빙산 위험·개수·현황(예: '지금 빙산 몇 개?') → get_iceberg_risk(icebergs_total/arctic 반환)
  · 현재 해양환경 종합(해빙·빙산·기상) → get_current_conditions
  · 출항 적기 추천 → recommend_departure   · 쇄빙선 함대 현황 → get_escort_status
  · 비용·경제성·수에즈 비교 → compare_economics(가능하면 departure_date 전달)
  · '정식 보고서/PDF/다운로드' → launch_full_report, '모든 시나리오' → launch_full_whatif
- 도구 지원 범위 주의: RIO(score_route)·경제성(compare_economics)·기상(get_route_weather)은 NSR/NWP/TSR
  (+비교용 SUEZ/CAPE)만 지원한다. 남극 ROSS/PENINSULA 는 쇄빙선 함대 정보(get_escort_status)만 제공되니,
  남극 항로의 RIO·비용·기상까지 가능한 것처럼 과대 약속하지 마라(가능한 범위만 정직하게 안내).

[계절·날짜 현실 — 매우 중요]
- score_route 결과에는 seasonal_assessment(navigability/season_level/note) 가 함께 온다. RIO 숫자가
  '안전'이어도 이 seasonal_assessment(특히 season_level=severe)를 '반드시' 답변에 반영하라.
- score_route 는 출항일이 속한 '월'의 해빙으로 평가하지만, 데이터 특성상 보수적이어서 겨울에도 RIO가
  '안전'으로 나올 수 있다. 도구 점수를 그대로 믿지 말고 '계절 현실'을 반드시 함께 반영하라:
  · 북극항로 항행 적기: **7~10월**(해빙 최소, 항행 창).
  · **12~4월은 극야(24h 어둠)·혹한(영하 30℃ 이하)·결빙 최성기** — 독자항행이 매우 어렵고 내빙보강·쇄빙선
    호위가 사실상 필수, 보험·비용도 급등한다.
- 따라서 같은 선박이라도 **12월은 6월보다 훨씬 위험·고난도**라고 분명히 답하라(도구가 같은 점수를 줘도).
  RIO 도구 결과가 계절 상식과 어긋나면, 계절 현실을 우선해 솔직히 설명하라('도구상 RIO는 보수적' 명시).

[경제성 결과 해석 — 비용/경제성을 다룰 때만 적용]
- 호위비는 compare_economics 의 escort_needed/escort_cost_usd 를 그대로 따른다(escort_needed=false→0,
  true→표시 금액). '호위비 $X (필요 없음)'처럼 금액·설명이 모순되게 쓰지 마라.
- 항해 소요일은 nsr.transit_days, 호위·보험은 북극 빙해 구간(arctic_transit_days)에만 부과된다.
  additional_cost_usd 는 (호위+보험)의 합이니 그 합을 '기타 비용'으로 또 적지 마라(중복 금지).
- 속도는 comparison.nsr_is_faster/time_saving_days(양수=NSR이 빠름)를 정확히 해석하라. NSR은 거리가 짧아
  대개 수에즈보다 빠르다. 결론은 균형있게: NSR의 가치=짧은 거리·빠른 도착·수에즈 통행료/혼잡/리스크 회피,
  상쇄 요인=호위·북극보험 같은 북극 프리미엄(계절·해빙에 따라 변동).
- 선박 제원을 일부만 주거나 안 줘도 합리적 기본값으로 도구를 호출하되, 사용한 가정을 답변에 명시하라.
- 도구가 오류(error 키)를 반환하면 솔직히 알리고 가능한 범위에서 답하라.
"""


class ChatAgent:
    """스트리밍 function-calling 대화 엔진. 세션별 대화 메모리를 보유한다."""

    def __init__(
        self,
        tool_schemas: list,
        dispatch: Callable[[str, dict], Awaitable[dict]],
    ):
        """
        tool_schemas: OpenAI function-calling 형식 도구 목록.
        dispatch    : async (tool_name, args) -> result dict. 결과에 '_job' 키가 있으면
                      job 이벤트로 프론트에 전달된다.
        """
        self._tool_schemas = tool_schemas
        self._dispatch = dispatch
        self._client: AsyncOpenAI | None = None
        self.sessions: dict[str, list[dict]] = {}

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY 환경변수가 설정되지 않았습니다. 챗봇에는 OpenAI API 키가 필요합니다."
                )
            self._client = AsyncOpenAI(api_key=api_key)
        return self._client

    def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def stream(self, session_id: str, user_message: str, ship_spec: dict | None = None):
        """SSE 이벤트(dict) 비동기 제너레이터.

        이벤트 종류:
          {"type":"token","text": ...}            — 답변 토큰
          {"type":"tool","name":...,"status":...} — 도구 호출 상태(running/done)
          {"type":"job","kind":...,"job_id":...}  — 백그라운드 작업 시작
          {"type":"done"}                          — 완료
          {"type":"error","detail": ...}           — 오류
        """
        history = self.sessions.setdefault(session_id, [])

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if ship_spec:
            messages.append({
                "role": "system",
                "content": "현재 UI에서 선택된 선박 제원(사용자가 따로 명시하지 않으면 기본값으로 사용): "
                           + json.dumps(ship_spec, ensure_ascii=False),
            })
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        final_text_parts: list[str] = []

        try:
            for _turn in range(MAX_TOOL_TURNS):
                content_buf: list[str] = []
                tool_acc: dict[int, dict] = {}

                stream = await self.client.chat.completions.create(
                    model=CHAT_MODEL,
                    messages=messages,
                    tools=self._tool_schemas,
                    tool_choice="auto",
                    temperature=0.4,
                    stream=True,
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content_buf.append(delta.content)
                        yield {"type": "token", "text": delta.content}
                    for tcd in (delta.tool_calls or []):
                        slot = tool_acc.setdefault(tcd.index, {"id": None, "name": "", "args": ""})
                        if tcd.id:
                            slot["id"] = tcd.id
                        if tcd.function:
                            if tcd.function.name:
                                slot["name"] = tcd.function.name
                            if tcd.function.arguments:
                                slot["args"] += tcd.function.arguments

                content_text = "".join(content_buf)
                tool_calls = [tool_acc[i] for i in sorted(tool_acc)]

                if not tool_calls:
                    # 도구 없이 최종 답변 완료
                    if content_text:
                        final_text_parts.append(content_text)
                    break

                # assistant(도구 호출) 메시지를 대화에 반영
                messages.append({
                    "role": "assistant",
                    "content": content_text or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["args"] or "{}"},
                        }
                        for tc in tool_calls
                    ],
                })

                # 각 도구 실행 → 결과를 tool 메시지로 피드백
                for tc in tool_calls:
                    name = tc["name"]
                    yield {"type": "tool", "name": name, "status": "running"}
                    try:
                        args = json.loads(tc["args"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await self._dispatch(name, args)

                    # 백그라운드 작업 신호 → job 이벤트
                    job = result.get("_job") if isinstance(result, dict) else None
                    if job:
                        yield {"type": "job", **job}

                    yield {"type": "tool", "name": name, "status": "done"}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                # 도구 루프 상한 도달 — 마지막으로 도구 없이 한 번 더 합성
                logger.warning("chat 도구 루프 상한(%d) 도달", MAX_TOOL_TURNS)

            final_text = "".join(final_text_parts).strip()
            # 세션 메모리에는 깔끔한 user/assistant 턴만 보존(도구 메시지는 비저장)
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": final_text or "(응답 없음)"})
            if len(history) > MAX_HISTORY_MESSAGES:
                del history[: len(history) - MAX_HISTORY_MESSAGES]

            yield {"type": "done"}

        except Exception as e:  # noqa: BLE001
            logger.error("chat 스트리밍 오류: %s", e, exc_info=True)
            yield {"type": "error", "detail": str(e)}
