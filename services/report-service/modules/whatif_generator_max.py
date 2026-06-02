"""
whatif_generator_max.py (v3.2)
==============================
Claude Agent SDK + Max 플랜 OAuth 인증 기반 What-If 시나리오 생성기.

v3 변경사항:
- POLARIS RIO 기반 분류 (avg_rio ≥ 1.5 → 추천, 0~1.5 → 조건부, < 0 → 비추천)
- 중복 시나리오 자동 제거 (route, ice_class, total_days, avg_rio, ice_multiplier 키)
- collected_route_summaries를 시나리오의 source of truth로 사용 (텍스트 파싱 의존 X)
- AI 텍스트는 comparison_text/ai_recommendation에 통째로 보존

v3.2 시나리오 구성 정책:
- Claude 도구 호출 결과는 dedupe 후 처음 CLAUDE_SCENARIO_CAP(3)개까지만 사용
  (사용자 원래 의도: Claude는 핵심 3개 = 기준/추천/비추천만 담당)
- 풀에서 random.randint(MIN_TOTAL_SCENARIOS=6, MAX_TOTAL_SCENARIOS=8) 만큼 보강
  (실질적으로 풀에서 3~5개를 무작위 추출)
- 매 호출마다 6~8개의 시나리오가 보장됨
- 풀 시나리오는 dedupe 키로 Claude 결과와 자동 중복 회피

요구사항:
- claude-agent-sdk 패키지
- nest_asyncio 패키지
- Claude Code CLI 설치 + Max 플랜 OAuth 로그인 완료
"""

import asyncio
import json
import logging
import os
import random
from datetime import date, timedelta

# Python 3.14에서 nest_asyncio는 asyncio Task 컨텍스트를 깨뜨림 — 비활성화
# import nest_asyncio

from claude_agent_sdk import (
    query, tool, create_sdk_mcp_server, ClaudeAgentOptions,
    AssistantMessage, TextBlock,
)

from .whatif_tools import WhatIfToolExecutor
from .whatif_generator import (
    WhatIfResult, ScenarioResult,
    WHATIF_SYSTEM_PROMPT, SCENARIO_PROMPT_TEMPLATE,
)
from .whatif_pool import HARDCODED_SCENARIO_POOL

logger = logging.getLogger("report-service.whatif_generator_max")

# nest_asyncio.apply()  # Python 3.14 호환성 — 비활성화


# ── 시나리오 구성 정책 (v3.2) ───────────────────────────────
# Claude는 핵심 3개(기준/추천/비추천)만 반영하고 나머지는 하드코딩 풀에서
# 무작위 3~5개를 추가하여 매 호출마다 6~8개의 시나리오가 보장되도록 한다.
#
# - Claude 도구 호출 결과는 dedupe 후 처음 CLAUDE_SCENARIO_CAP(3)개까지만 사용
# - 풀에서 random.randint(MIN_TOTAL_SCENARIOS, MAX_TOTAL_SCENARIOS) 만큼 채움
# - 풀 시나리오는 dedupe 키 기준으로 Claude 결과와 중복되지 않도록 자동 회피
CLAUDE_SCENARIO_CAP   = 3
MIN_TOTAL_SCENARIOS   = 6
MAX_TOTAL_SCENARIOS   = 8


def classify_recommendation_rio(rs: dict) -> str:
    """POLARIS RIO 기반 분류 (IMO 표준).

    avg_rio ≥ 1.5  → 추천 (단 safe_pct < 50%면 조건부 강등)
    0 ≤ avg_rio < 1.5 → 조건부
    avg_rio < 0     → 비추천 (Elevated Operations 영역)
    """
    avg_rio = rs.get('avg_rio')
    safe_pct = rs.get('safe_passage_pct', 0)

    if avg_rio is None:
        return '조건부'
    if avg_rio < 0.0:
        return '비추천'
    if avg_rio >= 1.5:
        return '조건부' if safe_pct < 50.0 else '추천'
    return '조건부'


def _summary_key(rs: dict) -> tuple:
    """시나리오 동등성 판정 키 (5축).

    동일 항로/선박/기간/RIO/해빙배율이면 같은 시나리오로 간주.
    """
    return (
        rs.get('route'),
        rs.get('ice_class'),
        rs.get('total_days'),
        rs.get('avg_rio'),
        rs.get('ice_multiplier'),
    )


def dedupe_summaries(summaries: list) -> list:
    """동일 시나리오 중복 제거.

    키 = (route, ice_class, total_days, avg_rio, ice_multiplier)
    """
    seen = set()
    unique = []
    for rs in summaries:
        key = _summary_key(rs)
        if key not in seen:
            seen.add(key)
            unique.append(rs)
    return unique


def augment_from_pool(
    summaries: list,
    base_route: str,
    base_ice_class: str,
    base_departure_date: str,
    base_forecast_days: int,
    tool_executor: WhatIfToolExecutor,
    target_min: int = MIN_TOTAL_SCENARIOS,
) -> int:
    """summaries가 target_min개 미만이면 하드코딩 풀에서 부족분만 보강한다.

    in-place로 summaries 리스트에 추가하며, 추가된 개수를 반환한다.
    이미 있는 dedupe key는 건너뛰므로 Claude 결과와 풀 결과 간 중복은 발생하지 않는다.
    """
    deduped = dedupe_summaries(summaries)
    needed = target_min - len(deduped)
    if needed <= 0:
        return 0

    seen_keys = {_summary_key(rs) for rs in deduped}

    try:
        base_dep_obj = date.fromisoformat(base_departure_date)
    except ValueError:
        base_dep_obj = date.today()

    pool = list(HARDCODED_SCENARIO_POOL)
    random.shuffle(pool)

    added = 0
    for template in pool:
        if added >= needed:
            break

        params = {
            'route': base_route,
            'ice_class': base_ice_class,
            'departure_date': base_departure_date,
            'forecast_days': base_forecast_days,
        }
        overrides = dict(template['overrides'])
        offset_days = overrides.pop('departure_offset_days', 0)
        if offset_days:
            params['departure_date'] = (
                base_dep_obj + timedelta(days=offset_days)
            ).isoformat()
        params.update(overrides)

        try:
            rs = tool_executor.execute(template['tool'], params)
        except Exception as e:
            logger.warning("풀 보강 실행 오류 [%s]: %s", template['name'], e)
            continue

        if not isinstance(rs, dict) or 'error' in rs or 'avg_rio' not in rs:
            continue

        key = _summary_key(rs)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # 풀에서 온 시나리오임을 식별할 수 있도록 라벨 부착
        rs.setdefault('scenario', template['name'])
        summaries.append(rs)
        added += 1

    if added > 0:
        logger.info("풀 보강 fallback: %d개 추가 (Claude 호출 부족)", added)
    return added


def parse_result_v3(ai_text: str, collected_summaries: list) -> WhatIfResult:
    """v3 파싱: 중복 제거 + RIO 기반 분류.

    텍스트 파싱 휴리스틱에 의존하지 않고, Claude가 호출한 도구 결과
    (collected_summaries)를 시나리오의 source of truth로 사용한다.
    """
    unique = dedupe_summaries(collected_summaries)

    scenarios = []
    for rs in unique:
        rec = classify_recommendation_rio(rs)

        route = rs.get('route', '?')
        ice = rs.get('ice_class', '?')
        scenario_label = rs.get('scenario', '')
        if scenario_label:
            name = f'{scenario_label} - {route}/{ice}'
        else:
            name = f'{route}/{ice} 기준'

        desc_parts = [f"avg_rio={rs.get('avg_rio')}"]
        if 'green_days' in rs:
            desc_parts.append(
                f"녹/황/적={rs.get('green_days')}/{rs.get('yellow_days')}/{rs.get('red_days')}일"
            )
        if 'safe_passage_pct' in rs:
            desc_parts.append(f"safe={rs.get('safe_passage_pct')}%")
        desc = ', '.join(desc_parts)

        scenarios.append(ScenarioResult(
            name=name, description=desc,
            route_summary=rs, recommendation=rec,
        ))

    ai_rec = ''
    for marker in ['## 종합 추천', '## 결론', '## 최종 권장', '## 최종 결론',
                    '# 종합', '# 결론', '## 최종', '## 권장']:
        idx = ai_text.find(marker)
        if idx >= 0:
            ai_rec = ai_text[idx:].strip()
            break
    if not ai_rec:
        ai_rec = ai_text[-1500:].strip()

    return WhatIfResult(
        scenarios=scenarios,
        comparison_text=ai_text,
        ai_recommendation=ai_rec,
    )


class WhatIfGeneratorMax:
    """v3.1: parse_result_v3 + 풀 보강 fallback. RIO 분류 + 중복 제거."""

    def __init__(self, route_scorer, data_loader):
        self.tool_executor = WhatIfToolExecutor(route_scorer, data_loader)
        self.collected_route_summaries = []
        self.tool_calls_count = 0
        # Max 플랜 OAuth 흐름에서는 API 키 환경변수가 있으면 SDK가 그쪽으로 우회하므로 제거
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def _make_tools(self):
        gen = self
        executor = self.tool_executor

        @tool("score_route", "특정 항로의 POLARIS RIO 안전도 평가", {
            "route": str, "ice_class": str, "departure_date": str, "forecast_days": int
        })
        async def score_route(args):
            gen.tool_calls_count += 1
            r = executor.execute("score_route", args)
            if "avg_rio" in r:
                gen.collected_route_summaries.append(r)
            return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}]}

        @tool("score_route_modified_ice", "해빙 농도 조정 시나리오", {
            "route": str, "ice_class": str, "departure_date": str,
            "forecast_days": int, "ice_multiplier": float
        })
        async def score_route_modified_ice(args) -> dict:
            gen.tool_calls_count += 1
            r = executor.execute("score_route_modified_ice", args)
            if "avg_rio" in r:
                gen.collected_route_summaries.append(r)
            return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}]}

        @tool("compare_ice_classes", "여러 Ice Class 비교", {
            "route": str, "departure_date": str, "forecast_days": int, "ice_classes": list
        })
        async def compare_ice_classes(args):
            gen.tool_calls_count += 1
            r = executor.execute("compare_ice_classes", args)
            for ic_summary in r.get("comparison", {}).values():
                if "avg_rio" in ic_summary:
                    gen.collected_route_summaries.append(ic_summary)
            return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}]}

        @tool("get_current_conditions", "현재 환경 데이터", {})
        async def get_current_conditions(args):
            gen.tool_calls_count += 1
            r = executor.execute("get_current_conditions", args)
            return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}]}

        return [score_route, score_route_modified_ice, compare_ice_classes, get_current_conditions]

    async def _async_generate(self, route, ice_class, departure_date, forecast_days):
        # Python 3.14 + nest_asyncio 환경에서 sniffio가 asyncio 감지 실패하는
        # 버그 우회: 명시적으로 ContextVar 설정.
        import sniffio
        try:
            sniffio.current_async_library_cvar.set("asyncio")
        except Exception:
            pass

        if not departure_date:
            departure_date = date.today().isoformat()

        prompt = SCENARIO_PROMPT_TEMPLATE.format(
            route=route, ice_class=ice_class,
            departure_date=departure_date, forecast_days=forecast_days,
        )

        tools = self._make_tools()
        server = create_sdk_mcp_server(name="whatif", version="1.0.0", tools=tools)

        options = ClaudeAgentOptions(
            mcp_servers={"whatif": server},
            allowed_tools=[
                "mcp__whatif__score_route",
                "mcp__whatif__score_route_modified_ice",
                "mcp__whatif__compare_ice_classes",
                "mcp__whatif__get_current_conditions",
            ],
            system_prompt=WHATIF_SYSTEM_PROMPT,
        )

        chunks = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)

        # ── v3.2 시나리오 구성 정책 ──────────────────────────────
        # Claude 도구 결과: dedupe 후 처음 CLAUDE_SCENARIO_CAP(3)개로 cap.
        # 풀 보강: random(MIN_TOTAL_SCENARIOS, MAX_TOTAL_SCENARIOS) 만큼 보강
        # 하여 매 호출마다 6~8개의 시나리오가 보장되도록 한다.
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
            "v3.2 구성 완료: Claude %d개 + 풀 %d개 = 총 %d개 (목표 %d개)",
            claude_count, added, len(self.collected_route_summaries), target,
        )

        return chr(10).join(chunks)

    def generate_scenarios(self, route="NSR", ice_class="PC5",
                           departure_date="", forecast_days=30):
        self.collected_route_summaries = []
        self.tool_calls_count = 0

        # Python 3.14 호환: nest_asyncio가 asyncio.run을 오버라이드하므로
        # 새 루프를 직접 생성하고 set_event_loop으로 현재 스레드에 바인딩.
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            ai_text = loop.run_until_complete(
                self._async_generate(route, ice_class, departure_date, forecast_days)
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        result = parse_result_v3(ai_text, self.collected_route_summaries)
        result.tool_calls_count = self.tool_calls_count
        return result
