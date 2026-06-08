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

디자인 시스템:
  - 표지/머리글/바닥글은 canvas 콜백(onFirstPage/onLaterPages)으로 직접 그린다.
  - 섹션 제목은 좌측 액센트 바 + 하단 구분선.
  - 현황 요약에는 KPI 카드 행.
  - 모든 차트는 통일된 matplotlib 테마(THEME)를 따른다.
"""

import io
import logging
import re
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

# ── 디자인 팔레트 ─────────────────────────────────────────────
NAVY      = "#0E2742"   # 표지 배경 / 헤더
NAVY_2    = "#16345C"   # 표지 상단 밴드
INK       = "#0F2A4A"   # 제목 텍스트
ACCENT    = "#2563EB"   # 포인트(블루)
SKY       = "#3B82F6"
PANEL     = "#F4F7FB"   # 카드/박스 배경
BORDER    = "#D7E0EA"   # 옅은 경계선
TEXT      = "#1F2A37"   # 본문
MUTED     = "#64748B"   # 보조 텍스트
GREEN     = "#16A34A"
AMBER     = "#F59E0B"
RED       = "#DC2626"
PURPLE    = "#7C3AED"

# ── 한국어 폰트 설정 ──────────────────────────────────────────
FONT_SEARCH_PATHS = [
    Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NanumGothic.ttf",
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("C:/Windows/Fonts/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/nanum/NanumGothic.ttf"),
]

_KO_FONT_NAME = "NanumGothic"
_KO_FONT_BOLD = "NanumGothic-Bold"
# 실제 bold 폰트(있으면) — <b> 태그가 시각적으로 굵게 렌더링되도록 등록한다.
# 없으면 regular 로 폴백(색상 강조로 보완).
BOLD_FONT_SEARCH_PATHS = [
    Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NanumGothicBold.ttf",
    Path("C:/Windows/Fonts/malgunbd.ttf"),
    Path("C:/Windows/Fonts/NanumGothicBold.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    Path("/usr/share/fonts/nanum/NanumGothicBold.ttf"),
]
# 실제로 사용 가능(등록 완료)한 폰트 이름. 한글 폰트를 못 찾으면 Helvetica로 폴백된다.
# Helvetica는 ReportLab 내장 폰트라 bold/italic 패밀리 매핑이 기본 제공되므로
# <para>/<b>/<i> 태그에서도 "Can't determine family" 에러가 나지 않는다.
KOREAN_FONT = "Helvetica"
KOREAN_FONT_BOLD = "Helvetica-Bold"
_ko_font_registered = False


def _apply_mpl_theme():
    """모든 차트에 공통 적용되는 matplotlib 스타일. font.family는 건드리지 않는다."""
    plt.rcParams.update({
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#CBD5E1",
        "axes.linewidth": 0.9,
        "axes.grid": False,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.titlecolor": INK,
        "axes.titlepad": 14,
        "axes.labelsize": 10,
        "axes.labelcolor": "#334155",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": "#475569",
        "ytick.color": "#475569",
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "grid.color": "#E2E8F0",
        "grid.linewidth": 0.8,
        "legend.fontsize": 9,
        "legend.frameon": False,
        "font.size": 10,
        "axes.unicode_minus": False,
    })


def _register_korean_font():
    """한글 폰트를 등록하고 실제 사용 가능한 폰트 이름을 반환한다."""
    global _ko_font_registered, KOREAN_FONT, KOREAN_FONT_BOLD
    if _ko_font_registered:
        return KOREAN_FONT
    _ko_font_registered = True  # 성공/실패와 무관하게 한 번만 시도

    for fpath in FONT_SEARCH_PATHS:
        if fpath.exists():
            try:
                pdfmetrics.registerFont(TTFont(_KO_FONT_NAME, str(fpath)))
                # 실제 bold 폰트가 있으면 등록 → <b> 태그가 시각적으로 굵어진다.
                bold_name = _KO_FONT_NAME
                for bpath in BOLD_FONT_SEARCH_PATHS:
                    if bpath.exists():
                        try:
                            pdfmetrics.registerFont(TTFont(_KO_FONT_BOLD, str(bpath)))
                            bold_name = _KO_FONT_BOLD
                            logger.info("한국어 bold 폰트 등록: %s", bpath)
                            break
                        except Exception as be:
                            logger.warning("bold 폰트 등록 실패 (%s): %s", bpath, be)
                KOREAN_FONT_BOLD = bold_name
                # bold/italic 변형 매핑. bold 는 실제 bold 폰트(없으면 regular 폴백).
                # 이게 없으면 <para>·<b>·<i> 태그 사용 시
                #   "Can't determine family/bold/italic for nanumgothic" 에러 발생.
                pdfmetrics.registerFontFamily(
                    _KO_FONT_NAME,
                    normal=_KO_FONT_NAME,
                    bold=bold_name,
                    italic=_KO_FONT_NAME,
                    boldItalic=bold_name,
                )
                # Matplotlib에도 등록
                fm.fontManager.addfont(str(fpath))
                plt.rcParams["font.family"] = fm.FontProperties(fname=str(fpath)).get_name()
                _apply_mpl_theme()
                KOREAN_FONT = _KO_FONT_NAME
                logger.info("한국어 폰트 등록: %s", fpath)
                return KOREAN_FONT
            except Exception as e:
                logger.warning("폰트 등록 실패 (%s): %s", fpath, e)

    # 한글 폰트를 끝내 못 찾음 → Helvetica 폴백.
    # (한글은 깨질 수 있으나 PDF 생성 자체는 크래시하지 않는다.)
    KOREAN_FONT = "Helvetica"
    _apply_mpl_theme()
    logger.warning("한국어 폰트를 찾지 못함 — Helvetica로 폴백합니다. PDF에서 한글이 깨질 수 있습니다.")
    return KOREAN_FONT


def _get_styles():
    """PDF 스타일 정의."""
    font = _register_korean_font()
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "KoTitle", fontName=font, fontSize=24, leading=30,
        alignment=1, spaceAfter=20, textColor=colors.HexColor(INK)
    ))
    styles.add(ParagraphStyle(
        "KoSubtitle", fontName=font, fontSize=14, leading=18,
        alignment=1, spaceAfter=10, textColor=colors.HexColor("#4a6a8a")
    ))
    # 섹션 제목(좌측 액센트 바 테이블 안에서 사용)
    styles.add(ParagraphStyle(
        "KoHeading", fontName=font, fontSize=15, leading=19,
        spaceBefore=0, spaceAfter=0, textColor=colors.HexColor(INK)
    ))
    styles.add(ParagraphStyle(
        "KoBody", fontName=font, fontSize=10, leading=15,
        spaceAfter=6, textColor=colors.HexColor(TEXT)
    ))
    # AI 텍스트 내부 markdown(### ) 소제목용
    styles.add(ParagraphStyle(
        "KoAiHeading", fontName=font, fontSize=11.5, leading=16,
        spaceBefore=8, spaceAfter=4, textColor=colors.HexColor(ACCENT),
    ))
    styles.add(ParagraphStyle(
        "KoSmall", fontName=font, fontSize=8, leading=11,
        textColor=colors.HexColor(MUTED)
    ))
    styles.add(ParagraphStyle(
        "KoBox", fontName=font, fontSize=10, leading=15,
        backColor=colors.HexColor(PANEL), borderPadding=10,
        leftIndent=2, rightIndent=2,
        borderWidth=0.5, borderColor=colors.HexColor(BORDER),
        spaceAfter=10, textColor=colors.HexColor(TEXT),
    ))
    # KPI 카드용
    styles.add(ParagraphStyle(
        "KpiValue", fontName=font, fontSize=17, leading=20,
        textColor=colors.HexColor(INK), spaceAfter=0,
    ))
    styles.add(ParagraphStyle(
        "KpiLabel", fontName=font, fontSize=8, leading=11,
        textColor=colors.HexColor(MUTED), spaceBefore=2,
    ))
    styles.add(ParagraphStyle(
        "KoCaption", fontName=font, fontSize=8.5, leading=12,
        textColor=colors.HexColor(MUTED), spaceBefore=2, spaceAfter=8,
        alignment=1,
    ))
    return styles


# ══════════════════════════════════════════════════════════════
# 표지 / 머리글·바닥글 (canvas 콜백)
# ══════════════════════════════════════════════════════════════

def _draw_cover(canvas, doc):
    """표지 페이지를 canvas로 직접 렌더링."""
    info = getattr(doc, "_cover_info", {})
    font = KOREAN_FONT
    w, h = A4
    canvas.saveState()

    # 배경
    canvas.setFillColor(colors.HexColor(NAVY))
    canvas.rect(0, 0, w, h, fill=1, stroke=0)
    # 상단 밴드
    canvas.setFillColor(colors.HexColor(NAVY_2))
    canvas.rect(0, h - 74 * mm, w, 74 * mm, fill=1, stroke=0)
    # 액센트 라인
    canvas.setFillColor(colors.HexColor(ACCENT))
    canvas.rect(0, h - 76 * mm, w, 2 * mm, fill=1, stroke=0)

    # 장식용 반투명 사각형 (모서리 포인트)
    canvas.setFillColor(colors.HexColor("#1B4170"))
    canvas.rect(w - 46 * mm, h - 46 * mm, 30 * mm, 30 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(ACCENT))
    canvas.rect(w - 46 * mm, h - 46 * mm, 30 * mm, 3 * mm, fill=1, stroke=0)

    # 아이브로우 라벨
    canvas.setFillColor(colors.HexColor("#8FBAEC"))
    canvas.setFont(font, 12)
    canvas.drawCentredString(w / 2, h - 42 * mm, "ARCTIC DIGITAL TWIN")

    # 메인 타이틀
    canvas.setFillColor(colors.white)
    canvas.setFont(font, 30)
    canvas.drawCentredString(w / 2, h * 0.55, "북극 항로 AI 동향 보고서")

    # 타이틀 하단 액센트 룰
    canvas.setFillColor(colors.HexColor(ACCENT))
    canvas.rect(w / 2 - 27 * mm, h * 0.55 - 8 * mm, 54 * mm, 1.5 * mm, fill=1, stroke=0)

    # 정보 라인
    canvas.setFillColor(colors.HexColor("#CBD9EC"))
    canvas.setFont(font, 13)
    y = h * 0.49
    for line in info.get("lines", []):
        canvas.drawCentredString(w / 2, y, line)
        y -= 8.5 * mm

    # RL 배지
    if info.get("rl_badge"):
        bw, bh = 52 * mm, 10 * mm
        bx, by = w / 2 - bw / 2, h * 0.34
        canvas.setFillColor(colors.HexColor("#14532D"))
        canvas.roundRect(bx, by, bw, bh, 2.2 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#86EFAC"))
        canvas.setFont(font, 10)
        canvas.drawCentredString(w / 2, by + 3.2 * mm, "RL 모델 학습 완료")

    # 바닥 밴드 + 푸터
    canvas.setFillColor(colors.HexColor(ACCENT))
    canvas.rect(0, 0, w, 9 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont(font, 9)
    canvas.drawCentredString(w / 2, 3.2 * mm, info.get("footer", ""))

    canvas.restoreState()


def _draw_later(canvas, doc):
    """본문 페이지 머리글/바닥글."""
    info = getattr(doc, "_cover_info", {})
    font = KOREAN_FONT
    w, h = A4
    canvas.saveState()

    # 머리글
    canvas.setFillColor(colors.HexColor(MUTED))
    canvas.setFont(font, 8)
    canvas.drawString(20 * mm, h - 13 * mm, "북극 항로 AI 동향 보고서")
    canvas.drawRightString(w - 20 * mm, h - 13 * mm, info.get("date_short", ""))
    canvas.setStrokeColor(colors.HexColor(BORDER))
    canvas.setLineWidth(0.6)
    canvas.line(20 * mm, h - 15 * mm, w - 20 * mm, h - 15 * mm)

    # 바닥글
    canvas.line(20 * mm, 14 * mm, w - 20 * mm, 14 * mm)
    canvas.setFillColor(colors.HexColor(MUTED))
    canvas.setFont(font, 8)
    canvas.drawString(20 * mm, 9 * mm, "Arctic Digital Twin · AI Generated Report")
    canvas.drawRightString(w - 20 * mm, 9 * mm, f"— {canvas.getPageNumber()} —")

    canvas.restoreState()


# ══════════════════════════════════════════════════════════════
# 차트 생성 함수
# ══════════════════════════════════════════════════════════════

def _fig_to_image(fig, width=CHART_WIDTH, height=CHART_HEIGHT):
    """Matplotlib Figure → ReportLab Image."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=width, height=height)


def _soft_grid(ax, axis="y"):
    """막대/선 차트용 옅은 그리드."""
    ax.set_axisbelow(True)
    ax.grid(axis=axis, color="#E2E8F0", linewidth=0.8)


def chart_monthly_ice(monthly_summary: list[dict]):
    """차트1: 월별 해빙 통계 (이중축 line+bar)."""
    months = [m["month"] for m in monthly_summary if m.get("available")]
    mean_concs = [m["mean_concentration"] for m in monthly_summary if m.get("available")]
    coverage = [m["arctic_coverage_pct"] for m in monthly_summary if m.get("available")]

    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax2 = ax1.twinx()

    bars = ax1.bar(months, coverage, alpha=0.85, color=SKY, label="북극 피복율(%)", width=0.6)
    line = ax2.plot(months, mean_concs, "o-", color=RED, linewidth=2.4,
                    markersize=6, markerfacecolor="white", markeredgewidth=1.6,
                    label="평균 농도")

    ax1.set_xlabel("월")
    ax1.set_ylabel("북극 피복율 (%)", color=SKY)
    ax2.set_ylabel("평균 해빙 농도", color=RED)
    ax1.set_xticks(months)
    ax1.set_xticklabels([f"{m}월" for m in months])
    ax2.spines["top"].set_visible(False)
    _soft_grid(ax1)

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

    ax.barh(y, green, color=GREEN, label="안전(Green)", height=0.6)
    ax.barh(y, yellow, left=green, color=AMBER, label="주의(Yellow)", height=0.6)
    ax.barh(y, red, left=[g + y_ for g, y_ in zip(green, yellow)], color=RED,
            label="위험(Red)", height=0.6)

    ax.set_yticks(y)
    ax.set_yticklabels(routes)
    ax.invert_yaxis()
    ax.set_xlabel("일수")
    ax.set_title("항로별 안전/주의/위험 일수 비교")
    ax.legend(loc="lower right", ncol=3)
    _soft_grid(ax, axis="x")
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_weather_radar(weather_routes: dict):
    """차트3: 기상 레이더 차트 (5개 항로)."""
    categories = ["파고(m)", "가시거리(km)", "기온(°C)", "해수면온도(°C)"]
    num_cats = len(categories)
    angles = np.linspace(0, 2 * np.pi, num_cats, endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
    ax.set_facecolor("#FBFCFE")
    route_colors = {"NSR": SKY, "NWP": GREEN, "TSR": PURPLE, "SUEZ": AMBER, "CAPE": RED}

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
        ax.plot(angles, values, "o-", linewidth=1.8, label=route_key, color=color, markersize=4)
        ax.fill(angles, values, alpha=0.12, color=color)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories)
    ax.set_ylim(0, 1)
    ax.set_yticklabels([])
    ax.grid(color="#D7E0EA", linewidth=0.8)
    ax.set_title("항로별 기상 조건 비교", pad=24)
    ax.legend(loc="upper right", bbox_to_anchor=(1.28, 1.1))
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
                ax.text(j, i, text, ha="center", va="center", fontsize=7, color="#1F2A37")

    ax.set_xticks(range(cols))
    ax.set_xticklabels([f"{d+1}" for d in range(cols)])
    ax.set_yticks(range(rows))
    ax.set_yticklabels([f"W{r+1}" for r in range(rows)])
    ax.set_title("출항 캘린더 (POLARIS RIO + RL 신뢰도)")
    ax.set_xlabel("일")
    ax.set_ylabel("주")
    ax.tick_params(length=0)
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
    ax.tick_params(length=0)
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

    bar_colors = [GREEN if c else RED for c in completed]
    ax.bar(stages, timesteps, color=bar_colors, alpha=0.9, width=0.6)

    for i, (s, ts, c) in enumerate(zip(stages, timesteps, completed)):
        status = "완료" if c else "실패"
        ax.text(i, ts + 1000, f"{ts:,}\n({status})", ha="center", fontsize=9, color="#334155")

    ax.set_xlabel("커리큘럼 단계")
    ax.set_ylabel("학습 스텝 수")
    ax.set_title("출항 RL 커리큘럼 학습 진행")
    _soft_grid(ax)
    fig.tight_layout()
    return _fig_to_image(fig)


def chart_avoidance_difficulty(difficulties: dict | None):
    """차트7: 구간별 빙산 회피 난이도 (RL(C) bar)."""
    if not difficulties:
        return None

    segments = list(difficulties.keys())
    values = list(difficulties.values())

    fig, ax = plt.subplots(figsize=(10, 5))
    bar_colors = [GREEN if v < 0.3 else AMBER if v < 0.6 else RED for v in values]
    bars = ax.bar(segments, values, color=bar_colors, width=0.6)

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width()/2, val + 0.02, f"{val:.2f}",
                ha="center", fontsize=9, color="#334155")

    ax.set_ylim(0, 1.1)
    ax.set_xlabel("NSR 구간")
    ax.set_ylabel("빙산 회피 난이도 (0=쉬움, 1=어려움)")
    ax.set_title("RL(SAC) 기반 구간별 빙산 회피 난이도")
    ax.axhline(y=0.3, color=GREEN, linestyle="--", alpha=0.6, label="안전 임계값")
    ax.axhline(y=0.6, color=AMBER, linestyle="--", alpha=0.6, label="주의 임계값")
    _soft_grid(ax)
    ax.legend()
    fig.tight_layout()
    return _fig_to_image(fig)


# ══════════════════════════════════════════════════════════════
# PDF 생성
# ══════════════════════════════════════════════════════════════

# 공통 테이블 스타일(헤더 네이비 + 줄무늬)
def _table_style(font):
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(INK)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
        ("LINEAFTER", (0, 0), (-2, -1), 0.4, colors.HexColor(BORDER)),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(PANEL)]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(BORDER)),
    ]
    return TableStyle(style)


def _inline_md(text):
    """인라인 markdown → ReportLab 미니 HTML.
    XML 특수문자를 먼저 이스케이프한 뒤 **굵게** 를 강조 태그로 변환한다.
    bold 폰트가 없을 수도 있으므로 색상까지 입혀 항상 눈에 띄게 한다."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # **굵게** / __굵게__ → 색상 강조 + bold
    text = re.sub(
        r"(?:\*\*|__)(.+?)(?:\*\*|__)",
        r'<b><font color="%s">\1</font></b>' % ACCENT,
        text,
    )
    # 잔여 단독 *, _ 강조 표식 제거(불릿 오인 방지 위해 굵게 처리 뒤에 수행)
    return text


def _md_to_flowables(text, styles, body_style):
    """AI 가 생성한 markdown 텍스트를 읽기 좋은 ReportLab 플로어블 리스트로 변환.
    - ### / ## / # → 소제목(KoAiHeading)
    - 빈 줄 → 단락 분리, 단일 줄바꿈 → <br/>
    - - / * / 1. → 불릿/번호 목록 (각자 줄바꿈)
    - **굵게** → 색상 강조
    문장 중간에 붙어온 '### ...' 헤더도 줄바꿈을 넣어 분리한다."""
    flow = []
    if not text or not text.strip():
        return flow

    # 헤더 마커가 줄 시작이 아니라 문장 중간에 붙어온 경우 줄바꿈으로 분리
    norm = re.sub(r"\s*(#{1,6})\s+", r"\n\1 ", text.replace("\r\n", "\n"))

    buf = []  # 연속 본문 줄 모으기

    def flush():
        if buf:
            html = "<br/>".join(_inline_md(b) for b in buf)
            if html.strip():
                flow.append(Paragraph(html, body_style))
            buf.clear()

    for raw in norm.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:                                   # 소제목
            heading = m.group(2).strip().rstrip("#").strip()
            # 헤더 치고 지나치게 길면 개행이 유실된 본문일 가능성 →
            # 잘못된 거대 제목 대신 본문으로 처리(### 마커만 제거).
            if len(heading) > 28:
                buf.append(heading)
            elif heading:
                flush()
                flow.append(Paragraph(_inline_md(heading), styles["KoAiHeading"]))
        elif re.match(r"^[-*]\s+", line):       # 불릿
            flush()
            item = re.sub(r"^[-*]\s+", "", line)
            flow.append(Paragraph("• " + _inline_md(item), body_style))
        elif re.match(r"^\d+\.\s+", line):      # 번호 목록
            flush()
            flow.append(Paragraph(_inline_md(line), body_style))
        else:
            buf.append(line)
    flush()
    return flow


class PdfGenerator:
    """PDF 보고서 생성기."""

    def __init__(self):
        self.styles = _get_styles()
        # 실제 등록된 폰트 이름 (한글 폰트 또는 Helvetica 폴백). 표 스타일에서 사용.
        self.font = KOREAN_FONT

    def _section_header(self, text):
        """좌측 액센트 바 + 하단 구분선이 있는 섹션 제목 플로어블."""
        p = Paragraph(text, self.styles["KoHeading"])
        t = Table([["", p]], colWidths=[3 * mm, 165 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(ACCENT)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (1, 0), (1, 0), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -1), 1.0, colors.HexColor(BORDER)),
        ]))
        return t

    def _kpi_cards(self, items):
        """KPI 카드 행. items = [(value, label, accent_color), ...] (4개 권장)."""
        cards = []
        for value, label, color in items:
            inner = Table(
                [[Paragraph(str(value), self.styles["KpiValue"])],
                 [Paragraph(str(label), self.styles["KpiLabel"])]],
                colWidths=[38 * mm],
            )
            inner.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(PANEL)),
                ("LINEABOVE", (0, 0), (-1, 0), 2.4, colors.HexColor(color)),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(BORDER)),
                ("TOPPADDING", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ]))
            cards.append(inner)

        gap = 4 * mm
        row, widths = [], []
        for i, c in enumerate(cards):
            if i:
                row.append("")
                widths.append(gap)
            row.append(c)
            widths.append(38 * mm)
        outer = Table([row], colWidths=widths)
        outer.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return outer

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
        ax.barh(x - w, greens, w, color=GREEN, label="안전(Green)")
        ax.barh(x, yellows, w, color=AMBER, label="주의(Yellow)")
        ax.barh(x + w, reds, w, color=RED, label="위험(Red)")
        ax.set_yticks(x)
        ax.set_yticklabels(names, fontsize=7)
        ax.set_xlabel("일수", fontsize=8)
        ax.set_title("What-If 시나리오 비교", fontsize=9, fontweight="bold")
        ax.legend(fontsize=7, loc="lower right")
        ax.invert_yaxis()
        ax.set_axisbelow(True)
        ax.grid(axis="x", color="#E2E8F0", linewidth=0.7)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
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
            topMargin=22*mm, bottomMargin=22*mm,
        )

        now = datetime.now()
        # 표지/머리글 정보를 canvas 콜백에서 사용
        cover_lines = [
            f"항로  {route}        빙급  {ice_class}",
            f"출항기간  {departure_date_start}  ~  {forecast_days}일",
            f"생성일시  {now.strftime('%Y-%m-%d %H:%M')}",
        ]
        doc._cover_info = {
            "lines": cover_lines,
            "rl_badge": bool(rl_model_info and rl_model_info.get("is_trained")),
            "footer": "Arctic Digital Twin · AI Generated Report",
            "date_short": now.strftime("%Y-%m-%d"),
        }

        story = []
        s = self.styles

        # ── 섹션 1: 표지 ──────────────────────────────────
        # 표지는 _draw_cover(canvas)가 그린다. 첫 페이지는 빈 페이지로 두고 넘긴다.
        story.append(PageBreak())

        # ── 섹션 2: 현황 요약 ─────────────────────────────
        story.append(self._section_header("1. 현재 북극해 현황 요약"))
        story.append(Spacer(1, 5*mm))

        # KPI 카드
        story.append(self._kpi_cards([
            (latest_ice_stats.get("mean_conc", "N/A"), "평균 해빙 농도", SKY),
            (f"{latest_ice_stats.get('high_conc_pct', 'N/A')}%", "고농도(≥80%) 비율", ACCENT),
            (berg_stats.get("total_count", "N/A"), "관측 빙산 수", PURPLE),
            (berg_stats.get("arctic_count", "N/A"), "북극권 빙산 수", RED),
        ]))
        story.append(Spacer(1, 6*mm))

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
        table.setStyle(_table_style(self.font))
        story.append(table)
        story.append(Spacer(1, 5*mm))
        story.extend(_md_to_flowables(ai_current, s, s["KoBody"]))
        story.append(PageBreak())

        # ── 섹션 3: 월별 해빙 동향 ───────────────────────
        story.append(self._section_header("2. 월별 해빙 동향"))
        story.append(Spacer(1, 4*mm))
        chart1 = chart_monthly_ice(monthly_summary)
        if chart1:
            story.append(chart1)
            story.append(Paragraph("그림 1. 월별 평균 해빙 농도 및 북극 피복율 추이", s["KoCaption"]))
        story.append(Spacer(1, 3*mm))

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
        table2.setStyle(_table_style(self.font))
        story.append(table2)
        story.append(Spacer(1, 5*mm))
        story.extend(_md_to_flowables(ai_monthly, s, s["KoBody"]))
        story.append(PageBreak())

        # ── 섹션 4: 항로별 위험도 비교 ───────────────────
        story.append(self._section_header("3. 항로별 위험도 비교"))
        story.append(Spacer(1, 4*mm))
        chart2 = chart_route_comparison(route_summary)
        if chart2:
            story.append(chart2)
            story.append(Paragraph("그림 2. 항로별 안전/주의/위험 일수", s["KoCaption"]))
        story.append(Spacer(1, 4*mm))

        weather_routes = weather_data.get("routes", {})
        chart3 = chart_weather_radar(weather_routes)
        if chart3:
            story.append(chart3)
            story.append(Paragraph("그림 3. 항로별 기상 조건 비교", s["KoCaption"]))
        story.append(PageBreak())

        # ── 섹션 5: RL 기반 출항 최적화 ──────────────────
        story.append(self._section_header("4. RL 기반 출항 최적화"))
        story.append(Spacer(1, 4*mm))
        chart4 = chart_departure_calendar(calendar, rl_departure_scores)
        if chart4:
            story.append(chart4)
            story.append(Paragraph("그림 4. 출항 캘린더 (POLARIS RIO + RL 신뢰도)", s["KoCaption"]))
        story.append(Spacer(1, 3*mm))

        if rl_calibration_info:
            cal_text = (
                f"예측 교정 RL: 에피소드 {rl_calibration_info.get('episode_count', 0)}회, "
                f"학습률 {rl_calibration_info.get('learning_rate', 'N/A')}"
            )
            story.append(Paragraph(cal_text, s["KoSmall"]))

        story.append(Spacer(1, 3*mm))
        story.extend(_md_to_flowables(ai_route, s, s["KoBody"]))
        story.append(PageBreak())

        # ── 섹션 6: 구간별 위험 분석 ─────────────────────
        story.append(self._section_header("5. 구간별 위험 분석"))
        story.append(Spacer(1, 4*mm))
        chart5 = chart_segment_heatmap(calendar)
        if chart5:
            story.append(chart5)
            story.append(Paragraph("그림 5. 구간별 POLARIS RIO 히트맵", s["KoCaption"]))
        story.append(Spacer(1, 4*mm))

        chart7 = chart_avoidance_difficulty(rl_avoidance_difficulties)
        if chart7:
            story.append(chart7)
            story.append(Paragraph("그림 6. RL(SAC) 기반 구간별 빙산 회피 난이도", s["KoCaption"]))
        story.append(PageBreak())

        # ── 섹션 7: What-If 시나리오 분석 (선택) ──────────
        if whatif_result and whatif_result.get("scenarios"):
            story.append(self._section_header("6. What-If 시나리오 분석"))
            story.append(Spacer(1, 4*mm))

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
            wt.setStyle(_table_style(self.font))
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
                story.extend(_md_to_flowables(rec, s, s["KoBox"]))

            story.append(PageBreak())

        # ── 섹션 8: AI 종합 결론 ─────────────────────────
        story.append(self._section_header("7. AI 종합 결론 및 권고사항"))
        story.append(Spacer(1, 5*mm))
        # 결론을 박스로 표시 (markdown 파싱: ### 소제목 / **강조** / 줄바꿈)
        story.extend(_md_to_flowables(ai_conclusions, s, s["KoBox"]))
        story.append(PageBreak())

        # ── 섹션 9: RL 모델 성능 ─────────────────────────
        story.append(self._section_header("8. RL 모델 성능 보고"))
        story.append(Spacer(1, 4*mm))
        chart6 = chart_rl_training_curve(rl_training_history)
        if chart6:
            story.append(chart6)
            story.append(Paragraph("그림 7. 출항 RL 커리큘럼 학습 진행", s["KoCaption"]))

        if rl_model_info:
            model_table_data = [
                ["항목", "값"],
                ["모델 상태", "학습 완료" if rl_model_info.get("is_trained") else "미학습"],
                ["모델 경로", str(rl_model_info.get("model_path", "N/A"))],
            ]
            mt = Table(model_table_data, colWidths=[60*mm, 100*mm])
            mt.setStyle(_table_style(self.font))
            story.append(Spacer(1, 5*mm))
            story.append(mt)

        # 빌드 (표지 + 머리글/바닥글 canvas 콜백)
        doc.build(story, onFirstPage=_draw_cover, onLaterPages=_draw_later)
        logger.info("PDF 생성 완료: %s", output_path)
        return output_path
