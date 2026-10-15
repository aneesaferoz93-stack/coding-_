"""
i18n/messages.py — English / Urdu / Pashto strings.

The USSD channel (*444#) is used by farmers without smartphones, and a USSD
screen holds about 160 characters. Pashto is the first language of most of the
pilot districts, so Pashto is the *default* for USSD and Urdu second — not the
other way round, which is how most Punjab-era portals were built and one reason
uptake in KP was poor.

Rule explanations are translated too: a Patwari who cannot read the reason for
a flag cannot act on it, and a flag nobody acts on is just noise.
"""

from __future__ import annotations

from typing import Any

MESSAGES: dict[str, dict[str, str]] = {
    "menu.root": {
        "en": "KP Land Integrity\n1 Check my land\n2 Report fraud\n3 Language\n4 Help",
        "ur": "KP لینڈ انٹیگریٹی\n1 زمین کی حیثیت\n2 فراڈ رپورٹ\n3 زبان\n4 مدد",
        "ps": "د KP د ځمکې بشپړتیا\n1 د ځمکې حالت\n2 درغلي خبر کړئ\n3 ژبه\n4 مرسته",
    },
    "menu.language": {
        "en": "Choose language\n1 Pashto\n2 Urdu\n3 English",
        "ur": "زبان منتخب کریں\n1 پشتو\n2 اردو\n3 انگریزی",
        "ps": "ژبه غوره کړئ\n1 پښتو\n2 اردو\n3 انګلیسي",
    },
    "menu.language_set": {
        "en": "Language set. Dial {code} again to continue.",
        "ur": "زبان تبدیل ہو گئی۔ جاری رکھنے کے لیے {code} دوبارہ ڈائل کریں۔",
        "ps": "ژبه وټاکل شوه. د دوام لپاره {code} بیا ووهئ.",
    },
    "menu.ask_khasra": {
        "en": "Enter Khasra number:",
        "ur": "خسرہ نمبر درج کریں:",
        "ps": "د خسرې نمبر ولیکئ:",
    },
    "menu.ask_mouza": {
        "en": "Enter Mouza code:",
        "ur": "موضع کوڈ درج کریں:",
        "ps": "د موضع کوډ ولیکئ:",
    },
    "menu.ask_cnic": {
        "en": "Enter your 13-digit CNIC:",
        "ur": "اپنا ۱۳ عددی شناختی کارڈ نمبر درج کریں:",
        "ps": "خپل ۱۳ رقمه شناختي کارډ ولیکئ:",
    },
    "status.clear": {
        "en": "OK. No fraud flag on this land. Ownership record matches.",
        "ur": "ٹھیک ہے۔ اس زمین پر کوئی فراڈ کا نشان نہیں۔",
        "ps": "سمه ده. په دې ځمکه د درغلۍ نښه نشته.",
    },
    "status.held": {
        "en": "HELD. This land is under review. Visit your Tehsildar office. Do not buy.",
        "ur": "رکا ہوا۔ یہ زمین زیرِ جانچ ہے۔ تحصیلدار دفتر جائیں۔ خریداری نہ کریں۔",
        "ps": "درول شوی. دا ځمکه تر څېړنې لاندې ده. تحصیلدار ته ورشئ. مه یې اخلئ.",
    },
    "status.blocked": {
        "en": "BLOCKED. Mutation stopped. Contact Deputy Commissioner office immediately.",
        "ur": "بند۔ انتقال روک دیا گیا۔ فوری طور پر ڈپٹی کمشنر دفتر رابطہ کریں۔",
        "ps": "بند شوی. انتقال ودرول شو. سمدستي د ډپټي کمشنر دفتر سره اړیکه ونیسئ.",
    },
    "status.unknown": {
        "en": "No record found. Check the Khasra number, or visit your Service Delivery Centre.",
        "ur": "کوئی ریکارڈ نہیں ملا۔ خسرہ نمبر چیک کریں یا سروس سینٹر جائیں۔",
        "ps": "هیڅ ریکارډ ونه موندل شو. د خسرې نمبر وګورئ یا خدماتو مرکز ته ورشئ.",
    },
    "report.received": {
        "en": "Report received. Ref no: {ref}. A Tehsildar will call you within 7 days.",
        "ur": "رپورٹ موصول ہوئی۔ حوالہ نمبر: {ref}۔ تحصیلدار ۷ دنوں میں رابطہ کریں گے۔",
        "ps": "راپور ترلاسه شو. حواله: {ref}. تحصیلدار به په ۷ ورځو کې اړیکه ونیسي.",
    },
    "help.text": {
        "en": "Free service. Dial {code}. Do not pay any agent. Helpline 1700.",
        "ur": "مفت سروس۔ {code} ڈائل کریں۔ کسی ایجنٹ کو پیسے نہ دیں۔ ہیلپ لائن ۱۷۰۰۔",
        "ps": "وړیا خدمت. {code} ووهئ. هیڅ اجنټ ته پیسې مه ورکوئ. مرستې کرښه ۱۷۰۰.",
    },
    "rule.R1": {
        "en": "Sold twice in 30 days",
        "ur": "۳۰ دنوں میں دو بار فروخت",
        "ps": "په ۳۰ ورځو کې دوه ځله خرڅ شوې",
    },
    "rule.R2": {
        "en": "Price far below government rate",
        "ur": "قیمت سرکاری ریٹ سے بہت کم",
        "ps": "بیه د حکومتي نرخ څخه ډېره ټیټه",
    },
    "rule.R3": {
        "en": "Seller dead or too old",
        "ur": "فروخت کنندہ فوت یا بہت بوڑھا",
        "ps": "خرڅوونکی مړ یا ډېر زوړ",
    },
    "rule.R4": {
        "en": "Attorney without thumb verification",
        "ur": "وکیل بغیر تصدیقِ بایومیٹرک",
        "ps": "وکیل د ګوتې نښې تصدیق پرته",
    },
    "rule.R5": {
        "en": "Name or clan differs from old register",
        "ur": "نام یا قوم پرانے ریکارڈ سے مختلف",
        "ps": "نوم یا قوم د پخواني ریکارډ سره توپیر لري",
    },
    "rule.R6": {
        "en": "No on-site visit by Patwari",
        "ur": "پٹواری نے موقع پر معائنہ نہیں کیا",
        "ps": "پټواري په ساحه کې معاینه نه ده کړې",
    },
    "rule.R7": {
        "en": "Same land sold to two people",
        "ur": "ایک زمین دو لوگوں کو فروخت",
        "ps": "یوه ځمکه دوه کسانو ته خرڅ شوې",
    },
    "rule.R8": {
        "en": "Unusual number of mutations by one official",
        "ur": "ایک اہلکار کی غیر معمولی تعداد",
        "ps": "د یوه مامور غیر معمولي شمېر",
    },
    "rule.R9": {
        "en": "Missing identity information",
        "ur": "شناختی معلومات نامکمل",
        "ps": "د پېژندنې معلومات نیمګړي",
    },
    "decision.CLEAR": {"en": "Clear", "ur": "صاف", "ps": "صفا"},
    "decision.HOLD": {"en": "Under review", "ur": "زیرِ غور", "ps": "تر غور لاندې"},
    "decision.BLOCK": {"en": "Stopped", "ur": "بند", "ps": "بند"},
    "decision.ADVISORY": {"en": "Advice only", "ur": "صرف مشورہ", "ps": "یوازې مشوره"},
}


def t(key: str, lang: str = "en", **kwargs: Any) -> str:
    """Translate ``key``. Falls back to English, then to the key itself."""
    bundle = MESSAGES.get(key, {})
    text = bundle.get(lang) or bundle.get("en") or key
    return text.format(**kwargs) if kwargs else text


def rule_label(rule_id: str, lang: str = "en") -> str:
    return t(f"rule.{rule_id}", lang)


def decision_label(decision: str, lang: str = "en") -> str:
    return t(f"decision.{decision}", lang)


LANGS = ("ps", "ur", "en")
