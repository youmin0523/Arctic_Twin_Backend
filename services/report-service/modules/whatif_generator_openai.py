"""
whatif_generator_openai.py (v4)
===============================
OpenAI (gpt-4o-mini) function-calling 기반 What-If 시나리오 생성기.

배경:
- 기존 whatif_generator_max.py 는 Claude Agent SDK + Max 플랜 OAuth(=Claude Code
  CLI 서브프로세스) 인증을 사용해, 헤드리스 AWS 배포 서버에서는 인증 세션이 없어
  POST /api/report/whatif 가 무한 대기(이벤트 루프 점유 → 응답 flush 불가)했다.
- 본 모듈은 일반 OPENAI_API_KEY 로 인증하는 AsyncOpenAI 를 사용하므로 서브프로세스
  없이 순수 async I/O 로 동작한다 → 헤드리스 서버에서 정상 작동 + 이벤트 루프 비점유.

설계 원칙 (기존과 100% 호환):
- 시나리오의 source of truth 는 AI 텍스트가 아니라 도구 실행 부수효과로 모이는
  collected_route_summaries + 풀 보강이다. 따라서 모델이 도구를 적게(혹은 전혀) 호출해도
  augment_from_pool 이 매 호출 6~8개를 보장한다.
- dedupe / 풀 보강 / 파싱 / 프롬프트 / 도구 실행기는 기존 모듈에서 그대로 재사용한다.

요구사항:
- openai 패키지 (>=1.0)
- 환경변수 OPENAI_API_KEY
- (선택) 환경변수 WHATIF_OPENAI_MODEL (기본 gpt-4o-mini)
"""

import json
import logging
import os
import random
from datetime import date

from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageFunctionToolCall

from .whatif_tools import WhatIfToolExecutor, TOOL_DEFINITIONS
from .whatif_generator import WHATIF_SYSTEM_PROMPT, SCENARIO_PROMPT_TEMPLATE
# 공유 헬퍼는 기존 max 모듈에서 그대로 재사용 (DRY)
from .whatif_generator_max import (
    dedupe_summaries,
    augment_from_pool,
    parse_result_v3,  # noqa: F401  (server.py 가 이 경로로도 import 가능하도록 재노출)
    CLAUDE_SCENARIO_CAP,
    MIN_TOTAL_SCENARIOS,
    MAX_TOTAL_SCENARIOS,
)

logger = logging.getLogger("report-service.whatif_generator_openai")

OPENAI_MODEL = os.environ.get("WHATIF_OPENAI_MODEL", "gpt-4o-mini")
MAX_TOOL_TURNS = 8  # 도구 호출 ↔ 결과 피드백 왕복 상한 (무한루프 방지)


def _to_openai_tools() -> list:
    """Anthropic 형식 TOOL_DEFINITIONS(input_schema) → OpenAI function-calling 형식 변환."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_DEFINITIONS
    ]


class WhatIfGeneratorOpenAI:
    """OpenAI function calling 기반 What-If 생성기.

    인터페이스(_async_generate / collected_route_summaries / tool_calls_count)는
    WhatIfGeneratorMax 와 동일하므로 server.py 의 _run_whatif 를 수정 없이 사용 가능.
    """

    def __init__(self, route_scorer, data_loader):
        self.tool_executor = WhatIfToolExecutor(route_scorer, data_loader)
        self.collected_route_summaries = []
        self.tool_calls_count = 0
        self._client = None
        self._tools = _to_openai_tools()

    @property
    def client(self) -> AsyncOpenAI:
        """지연 초기화 — import 시점이 아니라 첫 호출 시 키 검증."""
        if self._client is None:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY 환경변수가 설정되지 않았습니다. "
                    "What-If 분석에는 OpenAI API 키가 필요합니다."
                )
            self._client = AsyncOpenAI(api_key=api_key)
        return self._client

    def _dispatch_tool(self, name: str, args: dict) -> dict:
        """도구를 실행하고 route summary 부수효과를 수집 (Claude 버전과 동일 규칙)."""
        self.tool_calls_count += 1
        result = self.tool_executor.execute(name, args)

        if name == "compare_ice_classes":
            for ic_summary in result.get("comparison", {}).values():
                if isinstance(ic_summary, dict) and "avg_rio" in ic_summary:
                    self.collected_route_summaries.append(ic_summary)
        elif isinstance(result, dict) and "avg_rio" in result:
            self.collected_route_summaries.append(result)

        return result

    async def _async_generate(self, route, ice_class, departure_date, forecast_days,
                              progress_cb=None) -> str:
        """OpenAI 도구 호출 루프를 돌려 AI 텍스트를 반환하고, 시나리오를 수집/보강한다.

        progress_cb(pct:int) 가 주어지면 tool-call 루프 진행에 따라 점진적으로 호출해
        프론트 진행률이 한 지점에 멈춰 보이지 않도록 한다(체감 속도 개선).
        """
        def _p(pct: int):
            if progress_cb:
                try:
                    progress_cb(pct)
                except Exception:  # noqa: BLE001  진행률 갱신 실패는 본 작업에 영향 없음
                    pass

        if not departure_date:
            departure_date = date.today().isoformat()

        prompt = SCENARIO_PROMPT_TEMPLATE.format(
            route=route, ice_class=ice_class,
            departure_date=departure_date, forecast_days=forecast_days,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": WHATIF_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        _p(15)  # 첫 모델 호출 직전
        chunks = []
        for _turn in range(MAX_TOOL_TURNS):
            resp = await self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=cast(Any, messages),
                tools=cast(Any, self._tools),
                tool_choice="auto",
                temperature=0.7,
            )
            msg = resp.choices[0].message
            if msg.content:
                chunks.append(msg.content)

            tool_calls = msg.tool_calls or []
            if not tool_calls:
                _p(80)  # 모델이 최종 답변 → 생성 거의 완료
                break  # 모델이 도구 없이 최종 답변 → 종료

            # 매 턴 진행률 상승: 20 → 32 → 44 … (최대 78)
            _p(min(20 + _turn * 12, 78))

            # assistant 메시지(도구 호출 포함)를 대화에 반영
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": cast(ChatCompletionMessageFunctionToolCall, tc).function.name,
                            "arguments": cast(ChatCompletionMessageFunctionToolCall, tc).function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # 각 도구 실행 후 결과를 tool 메시지로 피드백
            for tc in tool_calls:
                fn_tc = cast(ChatCompletionMessageFunctionToolCall, tc)
                try:
                    args = json.loads(fn_tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._dispatch_tool(fn_tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        # ── v3.2 시나리오 구성 정책 (max 버전과 동일) ─────────────
        deduped = dedupe_summaries(self.collected_route_summaries)
        claude_count = len(deduped[:CLAUDE_SCENARIO_CAP])
        self.collected_route_summaries = deduped[:CLAUDE_SCENARIO_CAP]

        target = random.randint(MIN_TOTAL_SCENARIOS, MAX_TOTAL_SCENARIOS)
        added = augment_from_pool(
            self.collected_route_summaries,
            route, ice_class, departure_date, forecast_days,
            self.tool_executor,
            target_min=target,
        )
        logger.info(
            "v4(OpenAI/%s) 구성 완료: AI %d개 + 풀 %d개 = 총 %d개 (목표 %d개)",
            OPENAI_MODEL, claude_count, added,
            len(self.collected_route_summaries), target,
        )

        _p(85)  # 풀 보강까지 완료 (이후 server.py 가 90→100 마무리)
        return chr(10).join(chunks)
