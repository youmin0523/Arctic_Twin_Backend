"""
pdf_generator.py
================
ReportLab + Matplotlib 기반 한국어 PDF 보고서 생성.

차트 7종:
  1. 월별 해빙 통계 (이중축 line+bar)
  2. 항로별 위험도 비교 (수평 누적 bar)
  3. 기상 레이더 차트 (5개 항로)
  4. 출항 캘린더 히트맵 (RIO + RL 신뢰도)
  5. 구간별 위험도 히트맵 (RIO)
  6. RL 학습 곡선
  7. 구간별 빙산 회피 난이도 (RL(C) bar)

PDF 8개 섹션:
  1. 표지  2. 현황 요약  3. 월별 해빙 동향  4. 항로별 위험도 비교
  5. RL 출항 최적화  6. 구간별 위험 분석  7. AI 결론/권고  8. RL 모델 성능
"""

import io
import logging
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, KeepTogether,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger("report-service.pdf_generator")

PAGE_WIDTH, PAGE_HEIGHT = A4
CHART_WIDTH = 170 * mm
CHART_HEIGHT = 100 * mm

# ── 한국어 폰트 설정 ──────────────────────────────────────────
FONT_SEARCH_PATHS = [
    Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NanumGothic.ttf",
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("C:/Windows/Fonts/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/nanum/NanumGothic.ttf"),
]

_KO_FONT_NAME = "NanumGothic"
# 실제로 사용 가능(등록 완료)한 폰트 이름. 한글 폰트를 못 찾으면 Helvetica로 폴백된다.
# Helvetica는 ReportLab 내장 폰트라 bold/italic 패밀리 매핑이 기본 제공되므로
# <para>/<b>/<i> 태그에서도 "Can't determine family" 에러가 나지 않는다.
KOREAN_FONT = "Helvetica"
_ko_font_registered = False


def _register_korean_font():
    """한글 폰트를 등록하고 실제 사용 가능한 폰트 이름을 반환한다."""
    global _ko_font_registered, KOREAN_FONT
    if _ko_font_registered:
        return KOREAN_FONT
    _ko_font_registered = True  # 성공/실패와 무관하게 한 번만 시도

    for fpath in FONT_SEARCH_PATHS:
        if fpath.exists():
            try:
                pdfmetrics.registerFont(TTFont(_KO_FONT_NAME, str(fpath)))
                # bold/italic 변형 매핑 (단일 TTF 라 같은 폰트로 폴백)
                # 이게 없으면 <para>·<b>·<i> 태그 사용 시
                #   "Can't determine family/bold/italic for nanumgothic" 에러 발생.
                pdfmetrics.registerFontFamily(
                    _KO_FONT_NAME,
                    normal=_KO_FONT_NAME,
                    bold=_KO_FONT_NAME,
                    italic=_KO_FONT_NAME,
                    boldItalic=_KO_FONT_NAME,
                )
                # Matplotlib에도 등록
                fm.fontManager.addfont(str(fpath))
                plt.rcParams["font.family"] = fm.FontProperties(fname=str(fpath)).get_name()
                plt.rcParams["axes.unicode_minus"] = False
                KOREAN_FONT = _KO_FONT_NAME
                logger.info("한국어 폰트 등록: %s", fpath)
                return KOREAN_FONT
            except Exception as e:
                logger.warning("폰트 등록 실패 (%s): %s", fpath, e)

    # 한글 폰트를 끝내 못 찾음 → Helvetica 폴백.
    # (한글은 깨질 수 있으나 PDF 생성 자체는 크래시하지 않는다.)
    KOREAN_FONT = "Helvetica"
    logger.warning("한국어 폰트를 찾지 못함 — Helvetica로 폴백합니다. PDF에서 한글이 깨질 수 있습니다.")
    return KOREAN_FONT


def _get_styles():
    """PDF 스타일 정의."""
    font = _register_korean_font()
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "KoTitle", fontName=font, fontSize=24, leading=30,
        alignment=1, spaceAfter=20, textColor=colors.HexColor("#1a2a4a")
    ))
    styles.add(ParagraphStyle(
        "KoSubtitle", fontName=font, fontSize=14, leading=18,
        alignment=1, spaceAfter=10, textColor=colors.HexColor("#4a6a8a")
    ))
    styles.add(ParagraphStyle(
        "KoHeading", fontName=font, fontSize=16, leading=20,
        spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#1a2a4a")
    ))
    styles.add(ParagraphStyle(
        "KoBody", fontName=font, fontSize=10, leading=14,
        spaceAfter=6, textColor=colors.HexColor("#333333")
    ))
    styles.add(ParagraphStyle(
        "KoSmall", fontName=font, fontSize=8, leading=10,
        textColor=colors.HexColor("#666666")
    ))
    styles.add(ParagraphStyle(
        "KoBox", fontName=font, fontSize=10, leading=14,
        backColor=colors.HexColor("#f0f4f8"), borderPadding=8,
        borderWidth=1, borderColor=colors.HexColor("#c0d0e0"),
        spaceAfter=10, textColor=colors.HexColor("#333333"),
    ))
    return styles


# ══════════════════════════════════════════════════════════════
# 차트 생성 함수
# ══════════════════════════════════════════════════════════════

def _fig_to_image(fig, width=CHART_WIDTH, height=CHART_HEIGHT):
    """Matplotlib Figure → ReportLab Image."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=width, height=height)


def chart_monthly_ice(monthly_summary: list[dict]):
    """차트1: 월별 해빙 통계 (이중축 line+bar)."""
    months = [m["month"] for m in monthly_summary if m.get("available")]
    mean_concs = [m["mean_concentration"] for m in monthly_summary if m.get("available")]
    coverage = [m["arctic_coverage_pct"] for m in monthly_summary if m.get("available")]

    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax2 = ax1.twinx()

    bars = ax1.bar(months, coverage, alpha=0.3, color="#3b82f6", label="북극 피복율(%)")
    line = ax2.plot(months, mean_concs, "o-", color="#ef4444", linewidth=2, label="평균 농도")

    ax1.set_xlabel("월")
    ax1.set_ylabel("북극 피복율 (%)", color="#3b82f6")
    ax2.set_ylabel("평균 해빙 농도", color="#ef4444")
    ax1.set_xticks(months)
    ax1.set_xticklabels([f"{m}월" for m in months])

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right")
    ax1.set_title("월별 해빙 농도 및 북극 피복율")
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_route_comparison(route_summary: dict):
    """차트2: 항로별 위험도 비교 (수평 누적 bar)."""
    routes = list(route_summary.keys())
    green = [route_summary[r]["green_days"] for r in routes]
    yellow = [route_summary[r]["yellow_days"] for r in routes]
    red = [route_summary[r]["red_days"] for r in routes]

    fig, ax = plt.subplots(figsize=(10, 4))
    y = np.arange(len(routes))

    ax.barh(y, green, color="#22c55e", label="안전(Green)")
    ax.barh(y, yellow, left=green, color="#f59e0b", label="주의(Yellow)")
    ax.barh(y, red, left=[g + y_ for g, y_ in zip(green, yellow)], color="#ef4444", label="위험(Red)")

    ax.set_yticks(y)
    ax.set_yticklabels(routes)
    ax.set_xlabel("일수")
    ax.set_title("항로별 안전/주의/위험 일수 비교")
    ax.legend(loc="lower right")
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_weather_radar(weather_routes: dict):
    """차트3: 기상 레이더 차트 (5개 항로)."""
    categories = ["파고(m)", "가시거리(km)", "기온(°C)", "해수면온도(°C)"]
    num_cats = len(categories)
    angles = np.linspace(0, 2 * np.pi, num_cats, endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
    route_colors = {"NSR": "#3b82f6", "NWP": "#22c55e", "TSR": "#a855f7", "SUEZ": "#f59e0b", "CAPE": "#ef4444"}

    for route_key, route_data in weather_routes.items():
        s = route_data.get("summary", {})
        values = [
            min((s.get("max_wave_m") or 0) / 5, 1),
            min((s.get("min_vis_km") or 0) / 50, 1),
            max(0, min(((s.get("min_temp_c") or 0) + 30) / 60, 1)),
            max(0, min(((s.get("avg_sst_c") or 0) + 5) / 35, 1)),
        ]
        values += values[:1]
        color = route_colors.get(route_key, "#888888")
        ax.plot(angles, values, "o-", linewidth=1.5, label=route_key, color=color)
        ax.fill(angles, values, alpha=0.1, color=color)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories)
    ax.set_title("항로별 기상 조건 비교")
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1))
    fig.tight_layout()
    return _fig_to_image(fig, width=CHART_WIDTH, height=CHART_WIDTH * 0.8)


def chart_departure_calendar(calendar: list, rl_scores: dict | None = None):
    """차트4: 출항 캘린더 히트맵 (RIO + RL 신뢰도 오버레이)."""
    if not calendar:
        return None

    days = len(calendar)
    cols = 7
    rows = (days + cols - 1) // cols
    data = np.full((rows, cols), np.nan)
    rl_data = np.full((rows, cols), np.nan)

    for i, day_score in enumerate(calendar):
        r, c = divmod(i, cols)
        data[r, c] = day_score.overall_rio
        if rl_scores:
            rl_data[r, c] = rl_scores.get(day_score.date, np.nan)

    fig, ax = plt.subplots(figsize=(10, max(3, rows * 0.8)))
    im = ax.imshow(data, cmap="RdYlGn", aspect="auto", vmin=-10, vmax=2)

    for i in range(rows):
        for j in range(cols):
            idx = i * cols + j
            if idx < days:
                rio_val = data[i, j]
                text = f"{rio_val:.1f}"
                if not np.isnan(rl_data[i, j]):
                    text += f"\nRL:{rl_data[i,j]:.2f}"
                ax.text(j, i, text, ha="center", va="center", fontsize=7)

    ax.set_title("출항 캘린더 (POLARIS RIO + RL 신뢰도)")
    ax.set_xlabel("일")
    ax.set_ylabel("주")
    fig.colorbar(im, ax=ax, label="RIO 점수", shrink=0.8)
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_segment_heatmap(calendar: list):
    """차트5: 구간별 위험도 히트맵."""
    if not calendar or not calendar[0].segment_scores:
        return None

    segments = [s.name for s in calendar[0].segment_scores]
    days_count = len(calendar)
    data = np.zeros((len(segments), days_count))

    for j, day_score in enumerate(calendar):
        for i, seg in enumerate(day_score.segment_scores):
            data[i, j] = seg.rio

    fig, ax = plt.subplots(figsize=(12, 5))
    im = ax.imshow(data, cmap="RdYlGn", aspect="auto", vmin=-10, vmax=2)

    ax.set_yticks(range(len(segments)))
    ax.set_yticklabels(segments)
    ax.set_xlabel("날짜 (일)")
    ax.set_title("구간별 POLARIS RIO 히트맵")
    fig.colorbar(im, ax=ax, label="RIO", shrink=0.8)
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_rl_training_curve(training_history: list | None):
    """차트6: RL 학습 곡선."""
    if not training_history:
        return None

    fig, ax = plt.subplots(figsize=(10, 5))
    stages = [h["stage"] for h in training_history]
    timesteps = [h["timesteps"] for h in training_history]
    completed = [h.get("completed", False) for h in training_history]
    cum_steps = np.cumsum(timesteps)

    bar_colors = ["#22c55e" if c else "#ef4444" for c in completed]
    ax.bar(stages, timesteps, color=bar_colors, alpha=0.7)

    for i, (s, ts, c) in enumerate(zip(stages, timesteps, completed)):
        status = "완료" if c else "실패"
        ax.text(i, ts + 1000, f"{ts:,}\n({status})", ha="center", fontsize=9)

    ax.set_xlabel("커리큘럼 단계")
    ax.set_ylabel("학습 스텝 수")
    ax.set_title("출항 RL 커리큘럼 학습 진행")
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_avoidance_difficulty(difficulties: dict | None):
    """차트7: 구간별 빙산 회피 난이도 (RL(C) bar)."""
    if not difficulties:
        return None

    segments = list(difficulties.keys())
    values = list(difficulties.values())

    fig, ax = plt.subplots(figsize=(10, 5))
    bar_colors = ["#22c55e" if v < 0.3 else "#f59e0b" if v < 0.6 else "#ef4444" for v in values]
    bars = ax.bar(segments, values, color=bar_colors)

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width()/2, val + 0.02, f"{val:.2f}",
                ha="center", fontsize=9)

    ax.set_ylim(0, 1.1)
    ax.set_xlabel("NSR 구간")
    ax.set_ylabel("빙산 회피 난이도 (0=쉬움, 1=어려움)")
    ax.set_title("RL(SAC) 기반 구간별 빙산 회피 난이도")
    ax.axhline(y=0.3, color="#22c55e", linestyle="--", alpha=0.5, label="안전 임계값")
    ax.axhline(y=0.6, color="#f59e0b", linestyle="--", alpha=0.5, label="주의 임계값")
    ax.legend()
    fig.tight_layout()
    return _fig_to_image(fig)


# ══════════════════════════════════════════════════════════════
# PDF 생성
# ══════════════════════════════════════════════════════════════

class PdfGenerator:
    """PDF 보고서 생성기."""

    def __init__(self):
        self.styles = _get_styles()
        # 실제 등록된 폰트 이름 (한글 폰트 또는 Helvetica 폴백). 표 스타일에서 사용.
        self.font = KOREAN_FONT

    @staticmethod
    def _chart_whatif_comparison(scenarios: list[dict]):
        """What-If 시나리오 비교 그룹 바 차트."""
        if not scenarios:
            return None

        names = [sc.get("name", "")[:15] for sc in scenarios]
        greens = [sc.get("route_summary", {}).get("green_days", 0) for sc in scenarios]
        yellows = [sc.get("route_summary", {}).get("yellow_days", 0) for sc in scenarios]
        reds = [sc.get("route_summary", {}).get("red_days", 0) for sc in scenarios]

        fig, ax = plt.subplots(figsize=(6.5, 2.5))
        x = np.arange(len(names))
        w = 0.25
        ax.barh(x - w, greens, w, color="#27ae60", label="안전(Green)")
        ax.barh(x, yellows, w, color="#f39c12", label="주의(Yellow)")
        ax.barh(x + w, reds, w, color="#e74c3c", label="위험(Red)")
        ax.set_yticks(x)
        ax.set_yticklabels(names, fontsize=7)
        ax.set_xlabel("일수", fontsize=8)
        ax.set_title("What-If 시나리오 비교", fontsize=9, fontweight="bold")
        ax.legend(fontsize=7, loc="lower right")
        ax.invert_yaxis()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)

        return Image(buf, width=160*mm, height=65*mm)

    def generate(
        self,
        output_path: Path,
        route: str,
        ice_class: str,
        departure_date_start: str,
        forecast_days: int,
        transit_days: int,
        # 데이터
        monthly_summary: list[dict],
        latest_ice_stats: dict,
        berg_stats: dict,
        weather_data: dict,
        # 스코어링
        calendar: list,
        all_scores: dict,
        route_summary: dict,
        # AI 분석
        ai_monthly: str,
        ai_current: str,
        ai_route: str,
        ai_conclusions: str,
        # RL 결과 (선택)
        rl_departure_scores: dict | None = None,
        rl_training_history: list | None = None,
        rl_avoidance_difficulties: dict | None = None,
        rl_model_info: dict | None = None,
        rl_calibration_info: dict | None = None,
        whatif_result: dict | None = None,
    ) -> Path:
        """PDF 보고서 생성."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(output_path), pagesize=A4,
            leftMargin=20*mm, rightMargin=20*mm,
            topMargin=20*mm, bottomMargin=20*mm,
        )

        story = []
        s = self.styles

        # ── 섹션 1: 표지 ──────────────────────────────────
        story.append(Spacer(1, 60*mm))
        story.append(Paragraph("북극 항로 AI 동향 보고서", s["KoTitle"]))
        story.append(Spacer(1, 10*mm))
        story.append(Paragraph(
            f"항로: {route} | 빙급: {ice_class} | "
            f"출항기간: {departure_date_start} ~ {forecast_days}일",
            s["KoSubtitle"]
        ))
        story.append(Paragraph(
            f"생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            s["KoSubtitle"]
        ))
        if rl_model_info and rl_model_info.get("is_trained"):
            story.append(Paragraph("RL 모델: 학습 완료 ✓", s["KoSubtitle"]))
        story.append(PageBreak())

        # ── 섹션 2: 현황 요약 ─────────────────────────────
        story.append(Paragraph("1. 현재 북극해 현황 요약", s["KoHeading"]))

        # 해빙/빙산/기상 테이블
        status_data = [
            ["항목", "값"],
            ["북극권 관측 셀", str(latest_ice_stats.get("arctic_cells", "N/A"))],
            ["평균 해빙 농도", f"{latest_ice_stats.get('mean_conc', 'N/A')}"],
            ["고농도(≥80%) 비율", f"{latest_ice_stats.get('high_conc_pct', 'N/A')}%"],
            ["관측 빙산 수", str(berg_stats.get("total_count", "N/A"))],
            ["북극권 빙산 수", str(berg_stats.get("arctic_count", "N/A"))],
        ]
        table = Table(status_data, colWidths=[80*mm, 80*mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2a4a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), self.font),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c0d0e0")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4f8")]),
        ]))
        story.append(table)
        story.append(Spacer(1, 5*mm))
        story.append(Paragraph(ai_current, s["KoBody"]))
        story.append(PageBreak())

        # ── 섹션 3: 월별 해빙 동향 ───────────────────────
        story.append(Paragraph("2. 월별 해빙 동향", s["KoHeading"]))
        chart1 = chart_monthly_ice(monthly_summary)
        if chart1:
            story.append(chart1)
        story.append(Spacer(1, 5*mm))

        # 12행 테이블
        ice_table_data = [["월", "평균농도", "최대농도", "셀 수", "고농도(%)", "피복율(%)"]]
        for m in monthly_summary:
            if not m.get("available"):
                ice_table_data.append([f"{m['month']}월", "N/A", "N/A", "N/A", "N/A", "N/A"])
            else:
                ice_table_data.append([
                    f"{m['month']}월",
                    f"{m['mean_concentration']:.3f}",
                    f"{m['max_concentration']:.3f}",
                    str(m['cell_count']),
                    f"{m['high_concentration_pct']:.1f}",
                    f"{m['arctic_coverage_pct']:.1f}",
                ])
        table2 = Table(ice_table_data, colWidths=[20*mm, 28*mm, 28*mm, 25*mm, 30*mm, 30*mm])
        table2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2a4a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), self.font),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c0d0e0")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4f8")]),
        ]))
        story.append(table2)
        story.append(Spacer(1, 5*mm))
        story.append(Paragraph(ai_monthly, s["KoBody"]))
        story.append(PageBreak())

        # ── 섹션 4: 항로별 위험도 비교 ───────────────────
        story.append(Paragraph("3. 항로별 위험도 비교", s["KoHeading"]))
        chart2 = chart_route_comparison(route_summary)
        if chart2:
            story.append(chart2)
        story.append(Spacer(1, 5*mm))

        weather_routes = weather_data.get("routes", {})
        chart3 = chart_weather_radar(weather_routes)
        if chart3:
            story.append(chart3)
        story.append(PageBreak())

        # ── 섹션 5: RL 기반 출항 최적화 ──────────────────
        story.append(Paragraph("4. RL 기반 출항 최적화", s["KoHeading"]))
        chart4 = chart_departure_calendar(calendar, rl_departure_scores)
        if chart4:
            story.append(chart4)
        story.append(Spacer(1, 3*mm))

        if rl_calibration_info:
            cal_text = (
                f"예측 교정 RL: 에피소드 {rl_calibration_info.get('episode_count', 0)}회, "
                f"학습률 {rl_calibration_info.get('learning_rate', 'N/A')}"
            )
            story.append(Paragraph(cal_text, s["KoSmall"]))

        story.append(Paragraph(ai_route, s["KoBody"]))
        story.append(PageBreak())

        # ── 섹션 6: 구간별 위험 분석 ─────────────────────
        story.append(Paragraph("5. 구간별 위험 분석", s["KoHeading"]))
        chart5 = chart_segment_heatmap(calendar)
        if chart5:
            story.append(chart5)
        story.append(Spacer(1, 5*mm))

        chart7 = chart_avoidance_difficulty(rl_avoidance_difficulties)
        if chart7:
            story.append(chart7)
        story.append(PageBreak())

        # ── 섹션 7: What-If 시나리오 분석 (선택) ──────────
        if whatif_result and whatif_result.get("scenarios"):
            story.append(Paragraph("6. What-If 시나리오 분석", s["KoHeading"]))

            # 비교 테이블
            scenarios = whatif_result["scenarios"]
            tbl_data = [["시나리오", "평균 RIO", "안전일", "주의일", "위험일", "판정"]]
            for sc in scenarios:
                rs = sc.get("route_summary", {})
                tbl_data.append([
                    sc.get("name", "")[:20],
                    str(rs.get("avg_rio", "-")),
                    str(rs.get("green_days", "-")),
                    str(rs.get("yellow_days", "-")),
                    str(rs.get("red_days", "-")),
                    sc.get("recommendation", "-"),
                ])
            wt = Table(tbl_data, colWidths=[45*mm, 22*mm, 18*mm, 18*mm, 18*mm, 22*mm])
            wt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2a4a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), self.font),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c0d0e0")),
                ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ]))
            story.append(wt)
            story.append(Spacer(1, 5*mm))

            # What-If 비교 차트
            whatif_chart = self._chart_whatif_comparison(scenarios)
            if whatif_chart:
                story.append(whatif_chart)
                story.append(Spacer(1, 5*mm))

            # AI 종합 추천
            rec = whatif_result.get("ai_recommendation", "")
            if rec:
                story.append(Paragraph("AI 시나리오 종합 추천", s["KoSubtitle"]))
                for para in rec.split("\n\n"):
                    if para.strip():
                        story.append(Paragraph(para.strip(), s["KoBox"]))

            story.append(PageBreak())

        # ── 섹션 8: AI 종합 결론 ─────────────────────────
        story.append(Paragraph("7. AI 종합 결론 및 권고사항", s["KoHeading"]))
        # 결론을 박스로 표시
        for para in ai_conclusions.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), s["KoBox"]))
        story.append(PageBreak())

        # ── 섹션 9: RL 모델 성능 ─────────────────────────
        story.append(Paragraph("8. RL 모델 성능 보고", s["KoHeading"]))
        chart6 = chart_rl_training_curve(rl_training_history)
        if chart6:
            story.append(chart6)

        if rl_model_info:
            model_table_data = [
                ["항목", "값"],
                ["모델 상태", "학습 완료" if rl_model_info.get("is_trained") else "미학습"],
                ["모델 경로", str(rl_model_info.get("model_path", "N/A"))],
            ]
            mt = Table(model_table_data, colWidths=[60*mm, 100*mm])
            mt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2a4a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), self.font),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c0d0e0")),
            ]))
            story.append(Spacer(1, 5*mm))
            story.append(mt)

        # 빌드
        doc.build(story)
        logger.info("PDF 생성 완료: %s", output_path)
        return output_path
