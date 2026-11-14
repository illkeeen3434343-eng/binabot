"""All user-facing text (Azerbaijani). Nothing else in the project contains UI strings."""
from __future__ import annotations

import datetime as dt
import html
from zoneinfo import ZoneInfo

from .plans import ORDER, PLANS, get_plan

esc = html.escape

BTN_HOME, BTN_BACK = "🏠 Ana səhifə", "⬅️ Geri"
BTN_STATUS, BTN_LINKS, BTN_SUBS, BTN_HELP = "📊 Statusum", "🔗 Linklərim", "💎 Abunəlik", "ℹ️ Kömək"
BTN_ADMIN = "🛠 Admin panel"
BTN_ADD_LINK, BTN_EDIT, BTN_DELETE = "➕ Link əlavə et", "✏️ Dəyiş", "🗑 Sil"
BTN_YES, BTN_NO, BTN_CANCEL = "✅ Bəli", "❌ Xeyr", "❌ Ləğv et"
BTN_SEND_REQUEST = "✅ Sorğunu göndər"
BTN_APPROVE, BTN_REJECT = "✅ Təsdiqlə", "❌ Rədd et"

STATUS = {"new": "Abunəlik yoxdur", "pending": "⏳ Gözləmədə", "active": "✅ Aktiv",
          "rejected": "❌ Rədd edilib", "suspended": "⏸ Dayandırılıb", "expired": "⌛ Müddəti bitib"}

INVALID_LINK = "Yanlış link daxil etmisiniz. Zəhmət olmasa, yalnız Bina.az saytına aid keçid daxil edin."
UNSUPPORTED_LINK = ("Bu Bina.az linki hazırda dəstəklənmir.\n"
                    "Zəhmət olmasa, Bina.az-da axtarış edib (məs.: Bakı → Alqı-satqı → Mənzillər) "
                    "brauzerin ünvan sətrindəki axtarış linkini göndərin.\n\nSəbəb: <code>{detail}</code>")
DUPLICATE_LINK = "Bu link artıq siyahınızda var."
NOT_ACTIVE = "Link əlavə etmək üçün aktiv abunəliyiniz olmalıdır. «💎 Abunəlik» bölməsindən plan seçin."
LINK_LIMIT = "Planınız üzrə maksimum link sayına çatmısınız ({n}/{max}). Daha çox link üçün planı yüksəldin."
ASK_LINK = ("Bina.az-da istədiyiniz axtarışı edin və brauzerdəki linki bura göndərin.\n\n"
            "Nümunə:\n<code>https://bina.az/baki/alqi-satqi/menziller?room_ids[]=3&price_to=200000</code>")
ASK_NEW_LINK = "Köhnə linkin yerinə yeni Bina.az axtarış linkini göndərin."
LINK_ADDED = "✅ Link əlavə olundu. Bu axtarış üzrə yeni elanlar çıxdıqca sizə göndəriləcək."
LINK_UPDATED = "✅ Link yeniləndi."
LINK_DELETED = "🗑 Link silindi."
LINK_NOT_FOUND = "Link tapılmadı."
CONFIRM_EDIT = "Bu linki dəyişmək istədiyinizə əminsiniz?\n\n<code>{url}</code>"
CONFIRM_DELETE = "Bu linki silmək istədiyinizə əminsiniz?\n\n<code>{url}</code>"
CANCELLED = "Əməliyyat ləğv edildi."
LINK_PART = "⏳ Uzun link hissə-hissə gəlir, davamı gözlənilir…"
UNKNOWN_INPUT = "Zəhmət olmasa, aşağıdakı menyu düymələrindən istifadə edin."
NO_ACCESS = "Bu bölməyə icazəniz yoxdur."
IGNORED_PARAMS = "\n\nℹ️ Bu filtrlər nəzərə alınmır: <code>{params}</code>"


def fmt_dt(ts, tz: str, with_time=True) -> str:
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts, ZoneInfo(tz))
    return d.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def plan_title(key) -> str:
    p = get_plan(key)
    return p.title if p else "—"


def plans_table() -> str:
    rows = [f"• <b>{PLANS[k].title}</b> — {PLANS[k].max_links} link, saatda maksimum "
            f"{PLANS[k].ads_per_hour} elan" for k in ORDER]
    return "\n".join(rows)


def welcome(first_name, active: bool) -> str:
    t = (f"Salam, <b>{esc(first_name or 'dost')}</b>! 👋\n\n"
         "Mən Bina.az-da sizin axtarışınıza uyğun <b>yeni elanları</b> izləyir və dərhal sizə göndərirəm.\n\n")
    if active:
        return t + "Abunəliyiniz aktivdir. «🔗 Linklərim» bölməsindən axtarış linklərinizi idarə edin."
    return t + ("<b>Necə başlamaq olar?</b>\n"
                "1. «💎 Abunəlik» bölməsinə keçin və plan seçin.\n"
                "2. Sorğunuz adminə göndəriləcək (status: Gözləmədə).\n"
                "3. Admin təsdiqlədikdən sonra «🔗 Linklərim» bölməsində Bina.az axtarış linkinizi əlavə edin.\n\n"
                + plans_table())


def main_menu(u) -> str:
    return (f"🏠 <b>Ana səhifə</b>\n\nStatus: {STATUS.get(u['status'], u['status'])}\n"
            f"Plan: {plan_title(u['plan'])}\n\nMenyu aşağıda, klaviatura sahəsindədir 👇")


HELP = ("ℹ️ <b>Kömək</b>\n\n"
        "• <b>Statusum</b> — abunəlik vəziyyətiniz, bitmə tarixi və saatlıq limit.\n"
        "• <b>Linklərim</b> — izlənən Bina.az axtarış linkləri (əlavə et / dəyiş / sil).\n"
        "• <b>Abunəlik</b> — plan seçimi və aktivləşdirmə sorğusu.\n\n"
        "Bot yalnız link əlavə edildikdən <b>sonra</b> çıxan yeni elanları göndərir. "
        "Saatlıq limiti keçən elanlar itmir — növbəyə düşür və limit açıldıqca göndərilir.\n\n"
        + plans_table())


def status_card(u, n_links, sent_hour, queued, tz) -> str:
    p = get_plan(u["plan"])
    limit = p.ads_per_hour if p and u["status"] == "active" else 0
    lines = ["📊 <b>Statusum</b>", "",
             f"🆔 Telegram ID: <code>{u['user_id']}</code>",
             f"📌 Status: {STATUS.get(u['status'], u['status'])}",
             f"💎 Plan: {plan_title(u['plan'])}",
             f"🟢 Aktivləşmə tarixi: {fmt_dt(u['activated_at'], tz)}",
             f"📅 Bitmə tarixi: {fmt_dt(u['expires_at'], tz)}",
             f"🔗 Linklər: {n_links} / {p.max_links if p else 0}",
             f"📨 Son 1 saatda göndərilən: {sent_hour} / {limit}",
             f"📭 Qalan saatlıq limit: {max(0, limit - sent_hour)}"]
    if queued:
        lines.append(f"🕒 Növbədə gözləyən elan: {queued}")
    if u["requested_plan"]:
        lines.append(f"\n⏳ Gözləyən sorğu: {plan_title(u['requested_plan'])}")
    return "\n".join(lines)


def subs_screen(u) -> str:
    t = "💎 <b>Abunəlik planları</b>\n\n" + plans_table() + "\n\n"
    t += f"Cari plan: <b>{plan_title(u['plan'])}</b> ({STATUS.get(u['status'], u['status'])})\n"
    if u["requested_plan"]:
        t += f"⏳ Gözləyən sorğu: <b>{plan_title(u['requested_plan'])}</b>\n"
    return t + "\nPlan seçin:"


def subs_confirm(plan_key) -> str:
    p = PLANS[plan_key]
    return (f"<b>{p.title}</b> planı: {p.max_links} link, saatda maksimum {p.ads_per_hour} elan.\n\n"
            "Aktivləşdirmə sorğusu adminə göndərilsin?")


def request_sent(plan_key, payment_info) -> str:
    t = (f"✅ <b>{plan_title(plan_key)}</b> planı üçün sorğunuz adminə göndərildi.\n"
         "Təsdiqləndikdə sizə bildiriş gələcək.")
    return t + (f"\n\n💳 {esc(payment_info)}" if payment_info else "")


def links_screen(links, max_links) -> str:
    if not links:
        return f"🔗 <b>Linklərim</b> (0 / {max_links})\n\nHələ link əlavə etməmisiniz."
    return (f"🔗 <b>Linklərim</b> ({len(links)} / {max_links})\n\n"
            "Baxmaq, dəyişmək və ya silmək üçün linki seçin:")


def link_card(n, url, created, tz, paused=False) -> str:
    t = f"🔗 <b>Link {n}</b>\n\n<code>{show_url(url)}</code>\n\nƏlavə olunub: {fmt_dt(created, tz)}"
    return t + ("\n\n⏸ Bu link cari planın limitindən kənardadır və izlənmir." if paused else "")


def show_url(url, n=500) -> str:
    """Escaped link for a message body; very long links are cut (Telegram allows 4096 chars)."""
    return esc(url if len(url) <= n else url[:n] + "…")


def short_url(url, n=38) -> str:
    s = url.replace("https://bina.az/", "")
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------- notifications to users
def ad_caption(ad) -> str:
    def money(v):
        return f"{v:,}".replace(",", " ")
    lines = ["‼️ <b>Yeni elan</b>", ""]
    if ad.price is not None:
        lines.append(f"💰 Qiymət: {money(ad.price)} {esc((ad.currency or 'AZN').upper())}")
    if ad.rooms:
        lines.append(f"🚪 Otaq sayı: {ad.rooms}")
    if ad.area:
        area = int(ad.area) if float(ad.area).is_integer() else ad.area
        lines.append(f"📐 Sahə: {area} {esc(ad.area_units or 'm²')}")
    if ad.floor:
        lines.append(f"🏢 Mərtəbə: {ad.floor}/{ad.floors}" if ad.floors else f"🏢 Mərtəbə: {ad.floor}")
    if ad.location:
        lines.append(f"📍 Ünvan: {esc(ad.location[:300])}")
    lines += ["", f'🔗 <a href="{esc(ad.url, quote=True)}">Elana bax</a>']
    return "\n".join(lines)            # always far below Telegram's 1024-char caption limit


def limit_reached(plan_key, queued) -> str:
    p = get_plan(plan_key)
    t = f"⏱ Saatlıq limitə çatdınız ({p.ads_per_hour} elan/saat, {p.title})."
    if queued:
        t += f"\nNövbədə {queued} elan var — limit açıldıqca göndəriləcək."
    i = ORDER.index(p.key)
    if i + 1 < len(ORDER):
        n = PLANS[ORDER[i + 1]]
        t += (f"\n\n🚀 Daha çox elan almaq üçün <b>{n.title}</b> planına keçin: "
              f"{n.max_links} link, saatda {n.ads_per_hour} elan. «💎 Abunəlik» bölməsinə baxın.")
    return t


def approved(plan_key, expires, tz) -> str:
    return (f"🎉 Abunəliyiniz təsdiqləndi!\n\nPlan: <b>{plan_title(plan_key)}</b>\n"
            f"Bitmə tarixi: {fmt_dt(expires, tz)}\n\n«🔗 Linklərim» bölməsindən axtarış linki əlavə edin.")


REJECTED = "❌ Abunəlik sorğunuz rədd edildi. Ətraflı məlumat üçün adminlə əlaqə saxlayın."
SUSPENDED = "⏸ Abunəliyiniz admin tərəfindən müvəqqəti dayandırılıb."
REACTIVATED = "▶️ Abunəliyiniz yenidən aktivdir."
EXPIRED = ("⌛ Abunəliyinizin müddəti bitdi. Yeni elanlar göndərilməyəcək.\n"
           "Davam etmək üçün «💎 Abunəlik» bölməsindən yenidən sorğu göndərin.")
LINK_REMOVED_BY_ADMIN = "ℹ️ Bu linkiniz admin tərəfindən silindi:\n<code>{url}</code>"


def plan_changed(plan_key) -> str:
    return f"ℹ️ Planınız dəyişdirildi: <b>{plan_title(plan_key)}</b>."


def expiry_changed(expires, tz) -> str:
    return f"📅 Abunəliyinizin bitmə tarixi yeniləndi: {fmt_dt(expires, tz)}"


def expiry_reminder(days, expires, tz) -> str:
    return (f"⏰ Abunəliyinizin bitməsinə <b>{days} gün</b> qalıb ({fmt_dt(expires, tz)}).\n"
            "Fasiləsiz davam etmək üçün abunəliyi vaxtında yeniləyin.")


# ---------------------------------------------------------------- admin
def user_line(u) -> str:
    name = esc(u["first_name"] or "—")
    un = f" @{esc(u['username'])}" if u["username"] else ""
    return f"{name}{un} [<code>{u['user_id']}</code>]"


def admin_request(u) -> str:
    return (f"🆕 <b>Yeni abunəlik sorğusu</b>\n\n👤 {user_line(u)}\n"
            f"İstənilən plan: <b>{plan_title(u['requested_plan'])}</b>\n"
            f"Cari status: {STATUS.get(u['status'], u['status'])}")


def admin_user_card(u, n_links, sent_hour, queued, tz) -> str:
    return (f"👤 {user_line(u)}\n\n"
            f"Status: {STATUS.get(u['status'], u['status'])}\nPlan: {plan_title(u['plan'])}\n"
            f"Sorğu: {plan_title(u['requested_plan']) if u['requested_plan'] else '—'}\n"
            f"Qeydiyyat: {fmt_dt(u['registered_at'], tz)}\n"
            f"Aktivləşmə: {fmt_dt(u['activated_at'], tz)}\nBitmə: {fmt_dt(u['expires_at'], tz)}\n"
            f"Linklər: {n_links}\nSon 1 saatda göndərilən: {sent_hour}\nNövbədə: {queued}"
            + ("\n🚫 İstifadəçi botu bloklayıb" if u["blocked_bot"] else ""))


ASK_DATE = "Bitmə tarixini yazın (format: <code>GG.AA.İİİİ</code>, məs. <code>31.12.2026</code>):"
BAD_DATE = "Tarix düzgün deyil və ya keçmişdədir. Format: <code>GG.AA.İİİİ</code>"
CHOOSE_DURATION = "Abunəlik müddətini seçin:"
CHOOSE_PLAN = "Planı seçin:"
