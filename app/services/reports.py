"""Отчёт о работе SMM за период: цифры, график, выводы, гипотезы и действия.

ТЗ п. 3.6: «Отчёты должны содержать конкретные показатели, выводы и рекомендуемые
действия». Цифры считает services/analytics.py; выводы пишет LLM по этим цифрам
(промпт `weekly_report.md`). Если LLM недоступна или это mock — выводы строятся по
правилам, чтобы отчёт приходил всегда.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import tzinfo
from html import escape, unescape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import render_prompt
from app.config import IMAGES_DIR
from app.db.models import Business
from app.llm.base import LLMError, LLMProvider
from app.services import analytics
from app.services.analytics import AnalyticsSummary
from app.services.business_profile import parse_json_object, profile_to_text
from app.utils.time import utcnow

log = logging.getLogger(__name__)

REPORTS_DIR = IMAGES_DIR / "reports"

# Цвета графиков — фирменная палитра (основная Petrol + дополнительная для диаграмм)
CHART_BG = (255, 255, 255)
CHART_TEXT = (40, 80, 85)  # #285055
CHART_AXIS = (193, 208, 211)  # #C1D0D3
CHART_REACTIONS = (25, 108, 127)  # #196C7F Petrol
CHART_COMMENTS = (138, 204, 217)  # #8ACCD9
CHART_MUTED = (110, 130, 135)


@dataclass
class Insights:
    conclusions: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


@dataclass
class Report:
    text: str  # HTML для Telegram
    chart_path: Path | None = None
    summary: AnalyticsSummary | None = None
    insights: Insights = field(default_factory=Insights)

    @property
    def plain_text(self) -> str:
        import re

        return unescape(re.sub(r"<[^>]+>", "", self.text))


# ---------------------------------------------------------------- выводы


def rule_based_insights(s: AnalyticsSummary) -> Insights:
    """Выводы без LLM — по простым правилам."""
    ins = Insights()
    if s.posts_published == 0:
        ins.conclusions.append("За период не вышло ни одного поста — канал простаивает.")
        ins.actions.append("Утвердите 2–3 черновика или включите автономные черновики.")
        ins.hypotheses.append("Регулярные публикации 3 раза в неделю удержат внимание аудитории.")
        return ins

    ins.conclusions.append(
        f"Вышло {s.posts_published} пост(ов), в среднем {s.avg_reactions:g} реакций на пост."
    )
    if s.top_posts and s.top_posts[0]["reactions"] > 0:
        top = s.top_posts[0]
        ins.conclusions.append(f"Лучше всего зашёл пост #{top['id']} «{top['topic'][:50]}».")
        ins.actions.append(f"Сделайте продолжение темы «{top['topic'][:50]}».")
    if s.with_image_avg is not None and s.without_image_avg is not None:
        if s.with_image_avg > s.without_image_avg:
            ins.conclusions.append(
                f"Посты с картинкой собирают больше реакций ({s.with_image_avg:g} против "
                f"{s.without_image_avg:g})."
            )
            ins.actions.append("Добавляйте картинку к каждому посту.")
        elif s.without_image_avg > s.with_image_avg:
            ins.hypotheses.append("Текстовые посты заходят не хуже — проверьте формат «история».")
    elif s.with_image_avg is None:
        ins.hypotheses.append("Посты с картинками могут дать больше реакций — попробуйте 2 из 3.")
    if s.best_weekday:
        ins.hypotheses.append(
            f"Лучший отклик — в {s.best_weekday}: ставьте важные посты на этот день."
        )
    if s.comments_total:
        rate = round(100 * s.comments_replied / s.comments_total)
        ins.conclusions.append(f"Комментариев: {s.comments_total}, отвечено {rate}%.")
        if rate < 100:
            ins.actions.append("Ответьте на оставшиеся комментарии — /comments.")
    else:
        ins.hypotheses.append("Вопрос к подписчикам в конце поста поможет собрать комментарии.")
    if s.rejected > s.approved and s.rejected:
        ins.actions.append("Черновики чаще отклоняются — уточните тон и запреты в /profile.")
    if s.total_reactions == 0:
        ins.hypotheses.append(
            "Реакций нет: проверьте, включены ли реакции в канале и что бот — его администратор."
        )
    return ins


async def llm_insights(
    llm: LLMProvider, business: Business, summary: AnalyticsSummary
) -> Insights | None:
    if llm.name == "mock":
        return None
    system = render_prompt("weekly_report", profile=profile_to_text(business))
    user = json.dumps(analytics.summary_for_llm(summary), ensure_ascii=False, default=str)
    try:
        data = parse_json_object(
            await llm.complete(system, user, json_mode=True, task="weekly_report")
        )
    except (LLMError, ValueError) as exc:
        log.warning("Выводы отчёта от LLM не получены: %s", exc)
        return None

    def items(key: str) -> list[str]:
        value = data.get(key) or []
        return (
            [str(v).strip() for v in value if str(v).strip()][:4] if isinstance(value, list) else []
        )

    insights = Insights(items("conclusions"), items("hypotheses"), items("actions"))
    return insights if insights.conclusions or insights.actions else None


# ---------------------------------------------------------------- текст и график


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return f"\n\n<b>{title}</b>\n" + "\n".join(f"• {escape(line)}" for line in lines)


def render_text(s: AnalyticsSummary, ins: Insights) -> str:
    top = ""
    if s.top_posts and s.top_posts[0]["reactions"] + s.top_posts[0]["comments"] > 0:
        t = s.top_posts[0]
        top = (
            f"\n🏆 Лучший пост: #{t['id']} «{escape(t['topic'][:50])}» — "
            f"{t['reactions']} реакций, {t['comments']} комм."
        )
    replied = f", отвечено {s.comments_replied}" if s.comments_total else ""
    text = (
        f"📈 <b>Отчёт за {s.period_days} дн.</b> · {escape(s.company)}\n\n"
        f"📝 Опубликовано: <b>{s.posts_published}</b> (черновиков {s.drafts_created}, "
        f"одобрено {s.approved}, отклонено {s.rejected})\n"
        f"❤️ Реакции: <b>{s.total_reactions}</b>, в среднем {s.avg_reactions:g} на пост\n"
        f"💬 Комментарии: <b>{s.comments_total}</b>{replied}\n"
        f"💰 Расходы на AI: {s.costs_rub:.2f} ₽"
        f"{top}"
    )
    for platform, subscribers in s.subscribers.items():
        text += f"\n👥 Подписчики ({escape(platform)}): <b>{subscribers}</b>"
    if s.total_views is not None:
        text += f"\n👁 Просмотры: <b>{s.total_views}</b> по {s.posts_with_views} пост(ам)"
    text += _section("Выводы", ins.conclusions)
    text += _section("Гипотезы", ins.hypotheses)
    text += _section("Что сделать", ins.actions)
    if s.limitations:
        text += f"\n\n<i>{escape(s.limitations)}</i>"
    return text


def _font(size: int) -> ImageFont.ImageFont:
    for name in (
        "segoeui.ttf",  # фирменная замена по брендбуку (Windows)
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def chart_image(s: AnalyticsSummary) -> Image.Image:
    """Столбики «реакции + комментарии» по постам периода."""
    W, H, pad = 1000, 560, 70
    img = Image.new("RGB", (W, H), CHART_BG)
    d = ImageDraw.Draw(img)
    d.text((pad, 24), f"Отклик на посты за {s.period_days} дн. · {s.company}"[:70],
           font=_font(26), fill=CHART_TEXT)  # fmt: skip
    posts = s.posts[-10:]
    base_y, top_y = H - pad, 100
    d.line((pad, base_y, W - pad, base_y), fill=CHART_AXIS, width=2)
    if not posts:
        d.text((pad, H // 2), "Публикаций за период нет", font=_font(24), fill=CHART_MUTED)
    else:
        peak = max(max(p.reactions + p.comments for p in posts), 1)
        slot = (W - 2 * pad) / len(posts)
        bar = min(70, slot * 0.6)
        for i, p in enumerate(posts):
            x0 = pad + i * slot + (slot - bar) / 2
            h_r = (base_y - top_y) * p.reactions / peak
            h_c = (base_y - top_y) * p.comments / peak
            d.rectangle((x0, base_y - h_r, x0 + bar, base_y), fill=CHART_REACTIONS)
            d.rectangle((x0, base_y - h_r - h_c, x0 + bar, base_y - h_r), fill=CHART_COMMENTS)
            d.text((x0, base_y - h_r - h_c - 26), str(p.reactions + p.comments),
                   font=_font(18), fill=CHART_TEXT)  # fmt: skip
            d.text((x0, base_y + 8), f"#{p.id}", font=_font(18), fill=CHART_MUTED)
        d.rectangle((W - 330, 30, W - 312, 48), fill=CHART_REACTIONS)
        d.text((W - 304, 28), "реакции", font=_font(18), fill=CHART_TEXT)
        d.rectangle((W - 200, 30, W - 182, 48), fill=CHART_COMMENTS)
        d.text((W - 174, 28), "комментарии", font=_font(18), fill=CHART_TEXT)
    return img


def render_chart(s: AnalyticsSummary, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    chart_image(s).save(path)
    return path


async def build_report(
    session: AsyncSession,
    llm: LLMProvider,
    business: Business,
    days: int = 7,
    *,
    tz: tzinfo | None = None,
    out_dir: Path | None = None,
) -> Report:
    """Отчёт за последние `days` дней: текст (HTML) и график."""
    summary = await analytics.get_summary(session, business, days=days, tz=tz)
    insights = await llm_insights(llm, business, summary) or rule_based_insights(summary)
    chart = render_chart(
        summary,
        (out_dir or REPORTS_DIR) / f"report_{business.id}_{utcnow():%Y%m%d_%H%M%S}.png",
    )
    return Report(render_text(summary, insights), chart, summary, insights)
