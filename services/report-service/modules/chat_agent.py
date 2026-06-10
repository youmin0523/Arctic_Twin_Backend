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
너는 '북극 디지털 트윈 센터'의 북극항로 전용 항법 어시스턴트다. 한국어로 답한다.

[역할 범위 — 엄격히 한정]
- 너는 오직 북극항로 항행에 관한 질문에만 답한다. 다룰 수 있는 주제:
  · 북극/남극 항로(NSR 북동항로, NWP 북서항로, TSR 횡단극항로, 대안 SUEZ/CAPE, 남극 ROSS/PENINSULA)
  · 해빙 농도·해빙 두께·빙산, 파고·해수온·기상·가시거리
  · POLARIS RIO 위험도, Ice Class(PC1~PC7 / IA~IC / Arc4~9)
  · 쇄빙선 호위(아라온/CCGS/Rosatom), 출항 타이밍
  · 항로 타당성·총 부대비용·경제성(북극항로 vs 수에즈), 연료
- 위와 무관한 주제(요리·일반상식·코딩·정치 등)는 정중히 거절하고, 북극항로 관련 질문으로 유도하라.
  예: "죄송하지만 저는 북극항로 항행 관련 질문에만 답변할 수 있어요. 항로·기상·쇄빙선·비용 등 무엇이든 물어보세요."

[근거 사실]
- RIO 색상: green=안전, yellow=주의, red=위험. green 비율이 높을수록 항행 적합.
- Arc 등급은 IACS PC로 환산(Arc9≈PC3, Arc7≈PC4, Arc6≈PC5, Arc5≈PC6, Arc4≈PC7).
- 쇄빙선 함대: NSR=아라온(한국, Arc6), NWP=CCGS(캐나다, Arc7), TSR=원자력쇄빙선(러시아, Arc9).
- 호위비 산정: 쇄빙선 호위는 빙질이 선박의 독자 내빙능력을 '초과할 때만' 발생한다. RIO가 안전
  (escort_needed=false)하면 독자 항행이 가능하므로 호위비는 0이다 — 안전한데 호위비를 매기지 마라.
  필요할 때만 항차당 기준요금(자국 아라온=운영원가, 타국 Rosatom/CCGS=시장 수수료)을 위험도에 비례해 부과한다.
- 북극보험은 항차당 서차지(안전 시 ~$50k 기본, 위험할수록 상향)이며, 수에즈 통행료는 ~$500k다.
- 연료비는 본선(화물선) 연료만 계산되며 쇄빙선 연료는 호위 운영원가에 별도로 포함된다(중복 아님).

[경제성 결과 해석·서술 규칙 — 매우 중요]
- 항해 소요일은 compare_economics 의 nsr.transit_days(부산-로테르담 총 항해일, 예 ~23일)를 쓴다.
  score_route 가 주는 일수(예: 30일)는 RIO '평가 기간(forecast)'이지 항해 소요일이 아니다 — 혼동 금지.
- 호위비·북극보험은 전 항해일이 아니라 북극 빙해 구간(nsr.arctic_transit_days, 예 ~10일)에만 부과된다.
- additional_cost_usd 는 (호위비 + 북극보험)의 합이다. 호위비·보험을 항목으로 나열했다면 그 합계를
  '기타 추가비용'처럼 별도 항목으로 다시 적지 마라(중복 표기 금지). 총비용=연료비+호위비+북극보험.
- 속도 비교는 comparison.nsr_is_faster(true=NSR이 더 빠름) 또는 time_saving_days(양수=NSR이 그만큼
  빠름)를 정확히 해석하라. NSR은 거리가 짧아 대개 수에즈보다 빠르다 — '더 느리다'고 잘못 쓰지 마라.
- 결론은 한쪽으로 단정하지 말 것. NSR의 가치는 '짧은 거리·빠른 도착 + 수에즈 통행료·운하 혼잡·
  지정학 리스크 회피'에 있고, 호위·북극보험 같은 '북극 프리미엄'이 상쇄 요인이다(해빙 진행으로 점차
  감소). 따라서 시간 민감·고부가 화물엔 NSR이 유리할 수 있음을 속도 우위와 함께 균형있게 제시하라.

[오케스트레이션 규칙 — 매우 중요]
- '타당성', '총 부대비용', '갈 수 있는지', '경제성' 같은 종합 질문에는 **반드시 여러 도구를 연달아
  호출**해 근거를 모은 뒤 답하라. 전형적 조합:
  score_route(RIO 타당성) → get_current_conditions(현재 빙질) → compare_economics(총비용) →
  get_escort_status(쇄빙선 필요) → get_iceberg_risk(빙산 위험).
- 사용자가 선박 제원(크기·선종 등)을 일부만 주거나 안 줘도, 합리적 기본값으로 도구를 호출하되
  **사용한 가정(ice class, 속도, 거리 등)을 답변에 명시**하라.
- 최종 답변은 산만한 나열이 아니라, **수치 근거가 있는 하나의 결론**(권고/조건부/비추천)으로 합성하라.
- 사용자가 '정식 보고서', '다운로드', 'PDF', '모든 시나리오'처럼 정식 산출물을 원하면
  launch_full_report 또는 launch_full_whatif 를 호출해 백그라운드 작업을 시작하고,
  진행 중임을 알려라(결과는 잠시 후 패널에 표시된다).
- 도구가 오류(error 키)를 반환하면 그 사실을 솔직히 알리고 가능한 범위에서 답하라.
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
