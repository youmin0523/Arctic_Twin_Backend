"""
trend_analyzer.py
=================
OpenAI API(gpt-4o-mini)를 사용한 북극 항로 동향 분석 텍스트 생성.

4개 섹션별 독립 호출로 한국어 전문 분석 텍스트를 생성한다.
API 실패 시 fallback 텍스트를 반환하여 보고서 전체 실패를 방지한다.

배경:
- 기존에는 Anthropic Claude API(ANTHROPIC_API_KEY)를 사용했으나, 헤드리스 배포
  환경에서 키 인증 문제로 4개 섹션이 전부 폴백으로 떨어졌다.
- What-If 생성기(whatif_generator_openai)와 동일하게 OPENAI_API_KEY 기반 OpenAI
  클라이언트로 전환하여 인증 경로를 일원화한다.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("report-service.trend_analyzer")

# backend/.env에서 API 키 로드 — Digital_twin/backend/.env 우선, 단독 폴백
for _env in [
    Path(__file__).resolve().parents[3] / ".env",            # backend/services/report-service/modules → backend/.env
    Path(__file__).resolve().parents[3] / "backend" / ".env",
    Path(__file__).resolve().parents[2] / "backend" / ".env",
]:
    if _env.exists():
        load_dotenv(_env)
        break

SYSTEM_PROMPT = """당신은 북극 해양 항법 전문가이며, IMO POLARIS 방법론과 KR(한국선급) Polar Code에 정통합니다.
한국어로 전문적인 운항 보고서를 작성합니다.
다음 규칙을 따르세요:
- 전문 해양 용어를 정확히 사용하세요 (해빙 농도, 빙급, RIO 점수 등)
- 수치 데이터를 근거로 구체적인 분석을 제공하세요
- 안전 운항을 최우선으로 권고하세요
- 각 분석은 명확한 결론과 권고사항으로 마무리하세요"""

FALLBACK_TEXTS = {
    "monthly": "월별 해빙 동향 AI 분석을 수행할 수 없습니다. POLARIS RIO 수치 데이터를 참고하시기 바랍니다.",
    "current": "현재 해빙 현황 AI 분석을 수행할 수 없습니다. 최신 데이터 수치를 참고하시기 바랍니다.",
    "route": "항로별 위험 분석 AI 텍스트를 생성할 수 없습니다. 캘린더 데이터를 참고하시기 바랍니다.",
    "conclusions": "AI 종합 결론을 생성할 수 없습니다. 개별 섹션의 수치 데이터를 참고하시기 바랍니다.",
}


class TrendAnalyzer:
    """OpenAI API 기반 동향 분석기."""

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        # What-If 생성기와 동일하게 환경변수로 모델 override 가능 (기본 gpt-4o-mini)
        self.model = os.environ.get("TREND_OPENAI_MODEL", "gpt-4o-mini")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not self.api_key:
                logger.warning("OPENAI_API_KEY가 설정되지 않았습니다.")
                return None
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key)
            except Exception as e:
                logger.error("OpenAI 클라이언트 초기화 실패: %s", e)
                return None
        return self._client

    def _call_llm(self, user_prompt: str, max_tokens: int = 1500) -> str | None:
        """OpenAI Chat Completions 호출. 실패 시 None 반환."""
        if not self.client:
            return None
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error("OpenAI API 호출 실패: %s", e)
            return None

    def analyze_monthly_trends(self, monthly_summary: list[dict]) -> str:
        """월별 해빙 동향 분석 (계절별 패턴)."""
        # 데이터 테이블 구성
        table_lines = ["월 | 평균농도 | 최대농도 | 고농도비율(%) | 북극피복율(%)"]
        table_lines.append("---|---------|---------|-------------|------------")
        for m in monthly_summary:
            if not m.get("available"):
                table_lines.append(f"{m['month']:2d}월 | 데이터 없음")
                continue
            table_lines.append(
                f"{m['month']:2d}월 | {m['mean_concentration']:.3f} | "
                f"{m['max_concentration']:.3f} | "
                f"{m['high_concentration_pct']:.1f} | "
                f"{m['arctic_coverage_pct']:.1f}"
            )
        table = "\n".join(table_lines)

        prompt = f"""다음은 최근 12개월간의 북극 해빙 농도 월별 통계입니다:

{table}

다음 내용을 3~4단락으로 분석해 주세요:
1. 계절별 해빙 패턴 (하절기 해빙 최소기, 동절기 최대기)
2. 전년 대비 특이 사항이 있다면 언급
3. 북극 항로(NSR, NWP, TSR) 운항에 미치는 영향
4. 향후 수개월간의 해빙 예측 전망"""

        result = self._call_llm(prompt)
        return result or FALLBACK_TEXTS["monthly"]

    def analyze_current_conditions(
        self,
        ice_stats: dict,
        berg_stats: dict,
        weather_summary: dict,
    ) -> str:
        """현재 해빙·빙산·기상 현황 분석."""
        prompt = f"""현재 북극해 현황 데이터입니다:

【해빙 현황】
- 북극권(65°N+) 관측 셀: {ice_stats.get('arctic_cells', 'N/A')}개
- 평균 농도: {ice_stats.get('mean_conc', 'N/A')}
- 고농도(≥80%) 비율: {ice_stats.get('high_conc_pct', 'N/A')}%

【빙산 현황】
- 총 관측 빙산: {berg_stats.get('total_count', 'N/A')}개
- 북극권 빙산: {berg_stats.get('arctic_count', 'N/A')}개
- 평균 길이: {berg_stats.get('avg_length_m', 'N/A')}m
- 유형별 분포: {berg_stats.get('types', {})}

【기상 요약 (항로별)】
{_format_weather_summary(weather_summary)}

다음 내용을 2~3단락으로 분석해 주세요:
1. 현재 해빙 상태가 안전 운항에 미치는 영향
2. 빙산 분포 현황과 주의 구간
3. 기상 조건별 운항 주의사항 (파고, 가시거리, 기온)"""

        result = self._call_llm(prompt)
        return result or FALLBACK_TEXTS["current"]

    def analyze_route_risk(
        self,
        route: str,
        route_summary: dict,
        weather_route: dict | None,
    ) -> str:
        """항로별 위험 분석."""
        ws = ""
        if weather_route and "summary" in weather_route:
            s = weather_route["summary"]
            ws = f"""
【{route} 기상 요약】
- 최대 파고: {s.get('max_wave_m', 'N/A')}m
- 최저 기온: {s.get('min_temp_c', 'N/A')}°C
- 최저 가시거리: {s.get('min_vis_km', 'N/A')}km
- 평균 해수면 온도: {s.get('avg_sst_c', 'N/A')}°C"""

        prompt = f"""다음은 {route} 항로의 출항 캘린더 분석 결과입니다:

【RIO 점수 통계】
- 평균 RIO: {route_summary.get('avg_rio', 'N/A')}
- 최소 RIO: {route_summary.get('min_rio', 'N/A')}
- 최대 RIO: {route_summary.get('max_rio', 'N/A')}
- 안전(green) 일수: {route_summary.get('green_days', 0)}일
- 주의(yellow) 일수: {route_summary.get('yellow_days', 0)}일
- 위험(red) 일수: {route_summary.get('red_days', 0)}일
- 전체 분석 기간: {route_summary.get('total_days', 0)}일
{ws}

다음 내용을 2~3단락으로 분석해 주세요:
1. 이 항로의 전반적 위험도 평가
2. 안전 운항 가능 기간과 위험 기간
3. 해당 항로 운항 시 구체적 주의사항"""

        result = self._call_llm(prompt)
        return result or FALLBACK_TEXTS["route"]

    def write_conclusions(
        self,
        all_route_summaries: dict[str, dict],
        ice_class: str,
        transit_days: int,
    ) -> str:
        """종합 결론 및 권고사항."""
        routes_text = ""
        for route, summary in all_route_summaries.items():
            routes_text += (
                f"\n- {route}: 평균 RIO {summary['avg_rio']}, "
                f"안전 {summary['green_days']}일 / "
                f"주의 {summary['yellow_days']}일 / "
                f"위험 {summary['red_days']}일"
            )

        prompt = f"""다음은 북극 항로 종합 분석 결과입니다:

【선박 정보】
- 빙급: {ice_class}
- 예상 항행 일수: {transit_days}일

【항로별 위험도 요약】{routes_text}

다음 구조로 4~5단락의 종합 결론을 작성해 주세요:

1. 【권장 항로 및 출항 시기】 — 가장 안전한 항로와 최적 출항 기간
2. 【위험 경고】 — 반드시 피해야 할 기간과 항로
3. 【대체 항로 권고】 — 북극 항로가 위험할 경우의 대안 (수에즈, 희망봉)
4. 【운항 준비 권고】 — 필요한 장비, 보험, 쇄빙선 에스코트 등
5. 【종합 판단】 — 최종 운항 가/부 권고"""

        result = self._call_llm(prompt, max_tokens=2000)
        return result or FALLBACK_TEXTS["conclusions"]


def _format_weather_summary(weather: dict) -> str:
    """기상 요약을 텍스트로 포맷."""
    lines = []
    routes = weather.get("routes", {})
    for route_key in ["NSR", "NWP", "TSR", "SUEZ", "CAPE"]:
        if route_key not in routes:
            continue
        s = routes[route_key].get("summary", {})
        lines.append(
            f"  {route_key}: 파고 최대 {s.get('max_wave_m', '?')}m, "
            f"기온 최저 {s.get('min_temp_c', '?')}°C, "
            f"가시거리 최저 {s.get('min_vis_km', '?')}km"
        )
    return "\n".join(lines) if lines else "  기상 데이터 없음"
