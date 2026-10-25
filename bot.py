import os
import re
import math
import secrets
import sqlite3
import asyncio
import time
import threading
import datetime
import functools
import hashlib
import contextvars
from urllib.parse import urlparse, parse_qs

from flask import (
    Flask, request, redirect, url_for, session,
    render_template_string, jsonify,
)

from telethon import TelegramClient
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import InputStickerSetShortName

from telegram import (
    Update, MessageEntity, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from config import API_ID, API_HASH, BOT_TOKEN
try:
    from config import ADMIN_ID
except ImportError:
    ADMIN_ID = 0

user_client = TelegramClient("user_session", API_ID, API_HASH)
DB_PATH = "emoji_database.sqlite3"
PAGE_SIZE = 50
MAX_TEXT = 3500

# Telegram Bot API 9.4+ custom emoji icons for keyboard buttons.
BUTTON_ICON_IDS = {
    "Make a post": "5258331647358540449",
    "Add emoji": "5258108352008823107",
    "Remove emoji": "5275969776668134187",
    "View emoji ID": "5429571366384842791",
    "Favorite": "5226513232549664618",
    "Bot settings": "5258096772776991776",
    "Account": "5258513401784573443",
    "Refer": "5258362837411045098",
}


def green_inline_markup(rows):
    """Apply inline button styles for favorites and category management."""
    styled_rows = []
    for row in rows:
        styled_row = []
        for button in row:
            item = button.to_dict() if hasattr(button, "to_dict") else dict(button)
            label = str(item.get("text", "")).strip().lower()
            callback = str(item.get("callback_data", ""))

            if callback == "favorites":
                item["text"] = "Favorite"
                item["icon_custom_emoji_id"] = BUTTON_ICON_IDS["Favorite"]

            # Red actions
            if ("make a post" in label or label in {"📂 category", "📁 category"}
                    or callback in {"favorites", "category_add", "category_delete_confirm", "category_delete_no"}):
                item["style"] = "danger"
            # Blue actions
            elif callback == "post_replace_image":
                item["style"] = "primary"
            # Green actions
            elif ("previous" in label or "next" in label
                    or label in {"◀️", "▶️", "◀", "▶"}
                    or callback in {"favorite_add", "favorite_remove", "category_delete_yes", "post_next", "post_skip_image", "post_skip_button"}
                    or callback.startswith(("acat:", "ucat:"))):
                item["style"] = "success"
            else:
                item["style"] = "primary"
            item["text"] = tr_label(item["text"])
            styled_row.append(item)
        styled_rows.append(styled_row)
    return {"inline_keyboard": styled_rows}


def green_reply_markup(rows, resize_keyboard=True):
    """Return Bot API reply keyboard markup with green buttons."""
    styled_rows = []
    for row in rows:
        styled_row = []
        for button in row:
            item = button.to_dict() if hasattr(button, "to_dict") else dict(button)
            raw_text = str(item.get("text", ""))
            # The main-menu labels use plain text; Telegram displays the Premium emoji
            # from icon_custom_emoji_id instead of a normal Unicode emoji.
            clean_text = raw_text
            for prefix in ("📝 ", "➕ ", "➖ ", "🔎 "):
                if clean_text.startswith(prefix):
                    clean_text = clean_text[len(prefix):]
                    break
            if clean_text in BUTTON_ICON_IDS:
                item["text"] = clean_text
                item["icon_custom_emoji_id"] = BUTTON_ICON_IDS[clean_text]
            item["text"] = tr_label(item["text"])
            item["style"] = "success"
            styled_row.append(item)
        styled_rows.append(styled_row)
    return {"keyboard": styled_rows, "resize_keyboard": resize_keyboard}


# ---------------------------------------------------------------------------
# Main-menu button registry.
# Every button has a stable numeric ID so the admin can change its custom
# emoji, colour and position with /cng_emoji, /cng_calel and /cng_order.
# The registry is seeded into the database on first run.
# ---------------------------------------------------------------------------
DEFAULT_MENU_BUTTONS = [
    (1, "make_post", "Make a post", "5258331647358540449", "green"),
    (2, "add_emoji", "Add emoji", "5258108352008823107", "green"),
    (3, "view_emoji", "View emoji ID", "5429571366384842791", "green"),
    (4, "bot_settings", "Bot settings", "5258096772776991776", "green"),
    (5, "account", "Account", "5258513401784573443", "green"),
    (6, "refer", "Refer", "5258362837411045098", "green"),
    (7, "task", "Task", "", "green"),
    (8, "shop", "Shop", "", "green"),
    (9, "redeem_create", "Create redeem code", "", "green"),
    (10, "redeem_use", "Redeem code", "", "green"),
    (11, "tutorial", "Tutorial", "", "green"),
    (12, "support", "Support", "", "green"),
    (13, "my_order", "My order", "", "green"),
    (14, "order", "Order", "", "green"),
    (44, "language", "Language", "", "green"),
]

# Every menu (not only the main menu) has its own editable button registry.
# scope -> list of (key, default label). Category buttons are added dynamically.
DEFAULT_SCOPED_BUTTONS = {
    "category": [
        ("all_emoji", "All emoji"),
        ("favorite", "Favorite"),
        ("back", "Back"),
    ],
    "favorites": [
        ("add_favorite", "Add favorite"),
        ("remove_favorite", "Remove favorite"),
        ("back", "Back"),
    ],
    "bot_settings": [
        ("remove_emoji", "Remove emoji"),
        ("category", "\U0001F4C1 Category"),
        ("back", "Back"),
    ],
    "admin_categories": [
        ("create_category", "Create category"),
        ("back_to_menu", "Back to menu"),
    ],
    "admin_category_page": [
        ("add_to_category", "Add to category"),
        ("remove_from_category", "Remove from category"),
        ("delete_category", "Delete category"),
        ("back_to_categories", "Back to categories"),
    ],
    "shop": [
        ("buy_credit", "Buy credit"),
        ("buy_premium", "Buy premium"),
        ("buy_ff", "Buy FF like"),
        ("gen_redeem", "Generate redeem code"),
        ("back_to_main", "Back to main"),
    ],
    "payment": [
        ("bikash", "Bikash"),
        ("nagad", "Nagad"),
        ("other", "Other"),
        ("back_to_package", "Back to package"),
    ],
    "refer": [
        ("refer_history", "Refer history"),
        ("leader_board", "Leader board"),
        ("back_to_main", "Back to main"),
    ],
    "task": [
        ("claim_daily", "Claim daily reward"),
        ("back", "Back"),
    ],
}

COLOR_STYLES = {
    "normal": None, "default": None,
    "red": "danger", "danger": "danger",
    "blue": "primary", "primary": "primary",
    "green": "success", "success": "success",
}

MAX_POST_IMAGES = 5
MAX_POST_BUTTONS = 10
MAX_USER_CHANNELS = 5
FREE_POST_IMAGES = 1
FREE_POST_BUTTONS = 1
FAVORITE_LIMIT = 5
LOCK = "\U0001F512"

# ---------------------------------------------------------------------------
# Multi-language support (Bengali / English / Hindi / Arabic).
#
# Every outgoing button label and the core replies are passed through L(),
# which looks the English text up in the translation tables below.  The
# language of the user is kept in a ContextVar so concurrent updates never
# leak into each other, and incoming button taps are mapped back to the
# canonical English label with unL() before any menu logic runs.
# ---------------------------------------------------------------------------
LANG_ORDER = ["bn", "en", "hi", "ar"]
LANG_NAMES = {
    "bn": "\U0001F1E7\U0001F1E9 \u09AC\u09BE\u0982\u09B2\u09BE",
    "en": "\U0001F1EC\U0001F1E7 English",
    "hi": "\U0001F1EE\U0001F1F3 \u0939\u093F\u0928\u094D\u0926\u0940",
    "ar": "\U0001F1F8\U0001F1E6 \u0627\u0644\u0639\u0631\u0628\u064A\u0629",
}
LANG_LABEL_TO_CODE = {v: k for k, v in LANG_NAMES.items()}
DEFAULT_LANG = "en"
CURRENT_LANG = contextvars.ContextVar("current_lang", default=DEFAULT_LANG)

# Button labels: English canonical -> translations.
LABELS = {
    "Make a post": {"bn": "পোস্ট তৈরি করুন", "hi": "पोस्ट बनाएं", "ar": "إنشاء منشور"},
    "Add emoji": {"bn": "ইমোজি যোগ করুন", "hi": "इमोजी जोड़ें", "ar": "إضافة إيموجي"},
    "View emoji ID": {"bn": "ইমোজি আইডি দেখুন", "hi": "इमोजी आईडी देखें", "ar": "عرض معرف الإيموجي"},
    "Bot settings": {"bn": "বট সেটিংস", "hi": "बॉट सेटिंग्स", "ar": "إعدادات البوت"},
    "Account": {"bn": "অ্যাকাউন্ট", "hi": "खाता", "ar": "الحساب"},
    "Refer": {"bn": "রেফার", "hi": "रेफर", "ar": "الإحالة"},
    "Task": {"bn": "টাস্ক", "hi": "कार्य", "ar": "المهام"},
    "Shop": {"bn": "শপ", "hi": "दुकान", "ar": "المتجر"},
    "Create redeem code": {"bn": "রিডিম কোড তৈরি করুন", "hi": "रीडीम कोड बनाएं", "ar": "إنشاء رمز استرداد"},
    "Redeem code": {"bn": "রিডিম কোড", "hi": "रीडीम कोड", "ar": "رمز الاسترداد"},
    "Tutorial": {"bn": "টিউটোরিয়াল", "hi": "ट्यूटोरियल", "ar": "الشرح"},
    "Support": {"bn": "সাপোর্ট", "hi": "सहायता", "ar": "الدعم"},
    "My order": {"bn": "আমার অর্ডার", "hi": "मेरा ऑर्डर", "ar": "طلباتي"},
    "Order": {"bn": "অর্ডার", "hi": "ऑर्डर", "ar": "الطلبات"},
    "Language": {"bn": "ভাষা", "hi": "भाषा", "ar": "اللغة"},
    "Cancel": {"bn": "বাতিল", "hi": "रद्द करें", "ar": "إلغاء"},
    "Back": {"bn": "পিছনে", "hi": "वापस", "ar": "رجوع"},
    "Back to main": {"bn": "মেইন মেনুতে ফিরুন", "hi": "मुख्य मेनू पर जाएं", "ar": "العودة للقائمة"},
    "Back to menu": {"bn": "মেনুতে ফিরুন", "hi": "मेनू पर जाएं", "ar": "العودة للقائمة"},
    "Back to categories": {"bn": "ক্যাটাগরিতে ফিরুন", "hi": "श्रेणियों पर जाएं", "ar": "العودة للفئات"},
    "Back to package": {"bn": "প্যাকেজে ফিরুন", "hi": "पैकेज पर जाएं", "ar": "العودة للباقة"},
    "Next": {"bn": "পরবর্তী", "hi": "आगे", "ar": "التالي"},
    "Previous": {"bn": "আগের", "hi": "पिछला", "ar": "السابق"},
    "All emoji": {"bn": "সব ইমোজি", "hi": "सभी इमोजी", "ar": "كل الإيموجي"},
    "Favorite": {"bn": "ফেভারিট", "hi": "पसंदीदा", "ar": "المفضلة"},
    "Add favorite": {"bn": "ফেভারিট যোগ করুন", "hi": "पसंदीदा जोड़ें", "ar": "إضافة للمفضلة"},
    "Remove favorite": {"bn": "ফেভারিট সরান", "hi": "पसंदीदा हटाएं", "ar": "إزالة من المفضلة"},
    "Remove emoji": {"bn": "ইমোজি সরান", "hi": "इमोजी हटाएं", "ar": "إزالة الإيموجي"},
    "Category": {"bn": "ক্যাটাগরি", "hi": "श्रेणी", "ar": "الفئة"},
    "Create category": {"bn": "ক্যাটাগরি তৈরি করুন", "hi": "श्रेणी बनाएं", "ar": "إنشاء فئة"},
    "Add to category": {"bn": "ক্যাটাগরিতে যোগ করুন", "hi": "श्रेणी में जोड़ें", "ar": "إضافة للفئة"},
    "Remove from category": {"bn": "ক্যাটাগরি থেকে সরান", "hi": "श्रेणी से हटाएं", "ar": "إزالة من الفئة"},
    "Delete category": {"bn": "ক্যাটাগরি মুছুন", "hi": "श्रेणी हटाएं", "ar": "حذف الفئة"},
    "Buy credit": {"bn": "ক্রেডিট কিনুন", "hi": "क्रेडिट खरीदें", "ar": "شراء رصيد"},
    "Buy premium": {"bn": "প্রিমিয়াম কিনুন", "hi": "प्रीमियम खरीदें", "ar": "شراء بريميوم"},
    "Buy FF like": {"bn": "এফএফ লাইক কিনুন", "hi": "FF लाइक खरीदें", "ar": "شراء إعجابات FF"},
    "Generate redeem code": {"bn": "রিডিম কোড জেনারেট করুন", "hi": "रीडीम कोड बनाएं", "ar": "توليد رمز استرداد"},
    "Bikash": {"bn": "বিকাশ", "hi": "बिकाश", "ar": "بيكاش"},
    "Nagad": {"bn": "নগদ", "hi": "नगद", "ar": "ناغاد"},
    "Other": {"bn": "অন্যান্য", "hi": "अन्य", "ar": "أخرى"},
    "Refer history": {"bn": "রেফার হিস্টোরি", "hi": "रेफर इतिहास", "ar": "سجل الإحالات"},
    "Leaderboard": {"bn": "লিডারবোর্ড", "hi": "लीडरबोर्ड", "ar": "لوحة المتصدرين"},
    "Check join": {"bn": "জয়েন চেক করুন", "hi": "जॉइन जांचें", "ar": "تحقق من الانضمام"},
    "Claim 3 days premium": {"bn": "৩ দিনের প্রিমিয়াম নিন", "hi": "3 दिन का प्रीमियम लें", "ar": "احصل على بريميوم 3 أيام"},
    "Skip": {"bn": "বাদ দিন", "hi": "छोड़ें", "ar": "تخطي"},
    "Add more image": {"bn": "আরও ইমেজ যোগ করুন", "hi": "और इमेज जोड़ें", "ar": "إضافة صورة أخرى"},
    "Replace image": {"bn": "ইমেজ পরিবর্তন করুন", "hi": "इमेज बदलें", "ar": "تغيير الصورة"},
    "Add more button": {"bn": "আরও বাটন যোগ করুন", "hi": "और बटन जोड़ें", "ar": "إضافة زر آخر"},
    "Edit button": {"bn": "বাটন এডিট করুন", "hi": "बटन संपादित करें", "ar": "تعديل الزر"},
    "Button setting": {"bn": "বাটন সেটিং", "hi": "बटन सेटिंग", "ar": "إعداد الزر"},
    "Normal": {"bn": "সাধারণ", "hi": "सामान्य", "ar": "عادي"},
    "Red": {"bn": "লাল", "hi": "लाल", "ar": "أحمر"},
    "Blue": {"bn": "নীল", "hi": "नीला", "ar": "أزرق"},
    "Green": {"bn": "সবুজ", "hi": "हरा", "ar": "أخضر"},
    "Add channel": {"bn": "চ্যানেল যোগ করুন", "hi": "चैनल जोड़ें", "ar": "إضافة قناة"},
    "Remove channel": {"bn": "চ্যানেল সরান", "hi": "चैनल हटाएं", "ar": "إزالة القناة"},
    "Buy now": {"bn": "এখনই কিনুন", "hi": "अभी खरीदें", "ar": "اشترِ الآن"},
    "Active premium now": {"bn": "এখনই প্রিমিয়াম চালু করুন", "hi": "अभी प्रीमियम सक्रिय करें", "ar": "تفعيل بريميوم الآن"},
    "Load more emoji": {"bn": "আরও ইমোজি দেখুন", "hi": "और इमोजी देखें", "ar": "المزيد من الإيموجي"},
    "Check again": {"bn": "আবার চেক করুন", "hi": "फिर जांचें", "ar": "تحقق مرة أخرى"},
    "Continue": {"bn": "চালিয়ে যান", "hi": "जारी रखें", "ar": "متابعة"},
    "Claim now": {"bn": "এখনই নিন", "hi": "अभी लें", "ar": "احصل الآن"},
}

# Full replies used by the onboarding flow and the most common screens.
TEXTS = {
    "Welcome to Premium Emoji Bot\nPlease choose an option from the menu below.": {
        "bn": "প্রিমিয়াম ইমোজি বটে স্বাগতম\nনিচের মেনু থেকে একটি অপশন বেছে নিন।",
        "hi": "प्रीमियम इमोजी बॉट में आपका स्वागत है\nनीचे दिए मेनू से एक विकल्प चुनें।",
        "ar": "مرحبًا بك في بوت الإيموجي المميز\nاختر خيارًا من القائمة أدناه.",
    },
    "Choose your language / আপনার ভাষা নির্বাচন করুন": {
        "bn": "আপনার ভাষা নির্বাচন করুন",
        "hi": "अपनी भाषा चुनें",
        "ar": "اختر لغتك",
    },
    "Cancelled.": {"bn": "বাতিল করা হয়েছে।", "hi": "रद्द कर दिया गया।", "ar": "تم الإلغاء."},
    "You have already claimed your free premium.": {
        "bn": "আপনি ইতিমধ্যে আপনার ফ্রি প্রিমিয়াম নিয়েছেন।",
        "hi": "आप पहले ही अपना मुफ़्त प्रीमियम ले चुके हैं।",
        "ar": "لقد حصلت بالفعل على بريميوم المجاني.",
    },
    "❌ Could not read the pack. Please check that the link is valid.": {
        "bn": "❌ প্যাকটি পড়া যায়নি। লিংকটি সঠিক কিনা যাচাই করুন।",
        "hi": "❌ पैक पढ़ा नहीं जा सका। कृपया लिंक जाँचें।",
        "ar": "❌ تعذرت قراءة الحزمة. يرجى التحقق من الرابط.",
    },
    'No category selected.': {"bn": 'কোনো ক্যাটাগরি নির্বাচন করা হয়নি।', "hi": 'कोई श्रेणी चयनित नहीं है।', "ar": 'لم يتم اختيار أي فئة.'},
    'Send the new button name.': {"bn": 'নতুন বাটনের নাম পাঠান।', "hi": 'नया बटन नाम भेजें।', "ar": 'أرسل اسم الزر الجديد.'},
    'Send the new custom emoji ID.': {"bn": 'নতুন কাস্টম ইমোজি আইডি পাঠান।', "hi": 'नया कस्टम इमोजी आईडी भेजें।', "ar": 'أرسل معرف الإيموجي المخصص الجديد.'},
    'Enter a name for the new category.': {"bn": 'নতুন ক্যাটাগরির জন্য একটি নাম লিখুন।', "hi": 'नई श्रेणी के लिए नाम दर्ज करें।', "ar": 'أدخل اسمًا للفئة الجديدة.'},
    'Enter an ID or serial number to remove from this category.': {"bn": 'এই ক্যাটাগরি থেকে সরাতে একটি আইডি বা সিরিয়াল নম্বর লিখুন।', "hi": 'इस श्रेणी से हटाने के लिए आईडी या सीरियल नंबर दर्ज करें।', "ar": 'أدخل معرفًا أو رقمًا تسلسليًا لإزالته من هذه الفئة.'},
    'Send an emoji pack link, a message/forward containing Premium Emoji, or one or more IDs.': {"bn": 'ইমোজি প্যাক লিংক, প্রিমিয়াম ইমোজিসহ মেসেজ/ফরওয়ার্ড, অথবা এক বা একাধিক আইডি পাঠান।', "hi": 'इमोजी पैक लिंक, प्रीमियम इमोजी वाला संदेश/फ़ॉरवर्ड, या एक या अधिक आईडी भेजें।', "ar": 'أرسل رابط حزمة إيموجي، أو رسالة/إعادة توجيه تحتوي على إيموجي مميز، أو معرفًا واحدًا أو أكثر.'},
    'Send one or more Emoji IDs to remove them from the database.': {"bn": 'ডাটাবেজ থেকে সরাতে এক বা একাধিক ইমোজি আইডি পাঠান।', "hi": 'डेटाबेस से हटाने के लिए एक या अधिक इमोजी आईडी भेजें।', "ar": 'أرسل معرف إيموجي واحدًا أو أكثر لإزالتها من قاعدة البيانات.'},
    'Send an emoji pack link, an Emoji ID, or a message/forward containing Premium Emoji.': {"bn": 'ইমোজি প্যাক লিংক, একটি ইমোজি আইডি, অথবা প্রিমিয়াম ইমোজিসহ মেসেজ/ফরওয়ার্ড পাঠান।', "hi": 'इमोजी पैक लिंक, इमोजी आईडी, या प्रीमियम इमोजी वाला संदेश/फ़ॉरवर्ड भेजें।', "ar": 'أرسل رابط حزمة إيموجي، أو معرف إيموجي، أو رسالة/إعادة توجيه تحتوي على إيموجي مميز.'},
    'Main menu': {"bn": 'মেইন মেনু', "hi": 'मुख्य मेनू', "ar": 'القائمة الرئيسية'},
    'Please choose a colour: Normal, Red, Blue or Green.': {"bn": 'একটি রঙ বেছে নিন: সাধারণ, লাল, নীল বা সবুজ।', "hi": 'रंग चुनें: सामान्य, लाल, नीला या हरा।', "ar": 'اختر لونًا: عادي، أحمر، أزرق أو أخضر.'},
    'Draft not found. Please start again from Make a post.': {"bn": 'ড্রাফট পাওয়া যায়নি। অনুগ্রহ করে "পোস্ট তৈরি করুন" থেকে আবার শুরু করুন।', "hi": 'ड्राफ़्ट नहीं मिला। कृपया "पोस्ट बनाएं" से फिर से शुरू करें।', "ar": 'لم يتم العثور على المسودة. يرجى البدء من جديد من "إنشاء منشور".'},
    'No Premium Emoji IDs found. Please send or forward a message containing Premium Emoji.': {"bn": 'কোনো প্রিমিয়াম ইমোজি আইডি পাওয়া যায়নি। প্রিমিয়াম ইমোজিসহ একটি মেসেজ পাঠান বা ফরওয়ার্ড করুন।', "hi": 'कोई प्रीमियम इमोजी आईडी नहीं मिली। प्रीमियम इमोजी वाला संदेश भेजें या फ़ॉरवर्ड करें।', "ar": 'لم يتم العثور على معرفات إيموجي مميزة. أرسل أو أعد توجيه رسالة تحتوي على إيموجي مميز.'},
}


ACCOUNT_WORDS = {
    "en": {"title": "Account", "name": "Name", "username": "Username", "user_id": "User ID",
           "status": "Status", "premium": "Premium \u2B50", "normal": "Normal",
           "valid": "Premium valid until", "expired": "Premium expired on",
           "credit": "Credit", "language": "Language", "no_premium": "Premium: not active"},
    "bn": {"title": "\u0985\u09CD\u09AF\u09BE\u0995\u09BE\u0989\u09A8\u09CD\u099F", "name": "\u09A8\u09BE\u09AE", "username": "\u0987\u0989\u099C\u09BE\u09B0\u09A8\u09C7\u09AE", "user_id": "\u0987\u0989\u099C\u09BE\u09B0 \u0986\u0987\u09A1\u09BF",
           "status": "\u09B8\u09CD\u099F\u09CD\u09AF\u09BE\u099F\u09BE\u09B8", "premium": "\u09AA\u09CD\u09B0\u09BF\u09AE\u09BF\u09DF\u09BE\u09AE \u2B50", "normal": "\u09B8\u09BE\u09A7\u09BE\u09B0\u09A3",
           "valid": "\u09AA\u09CD\u09B0\u09BF\u09AE\u09BF\u09DF\u09BE\u09AE \u099A\u09B2\u09AC\u09C7", "expired": "\u09AA\u09CD\u09B0\u09BF\u09AE\u09BF\u09DF\u09BE\u09AE \u09B6\u09C7\u09B7 \u09B9\u09AF\u09BC\u09C7\u099B\u09C7",
           "credit": "\u0995\u09CD\u09B0\u09C7\u09A1\u09BF\u099F", "language": "\u09AD\u09BE\u09B7\u09BE",
           "no_premium": "\u09AA\u09CD\u09B0\u09BF\u09AE\u09BF\u09DF\u09BE\u09AE: \u099A\u09BE\u09B2\u09C1 \u09A8\u09C7\u0987"},
    "hi": {"title": "\u0916\u093E\u0924\u093E", "name": "\u0928\u093E\u092E", "username": "\u092F\u0942\u091C\u093C\u0930\u0928\u0947\u092E", "user_id": "\u092F\u0942\u091C\u093C\u0930 \u0906\u0908\u0921\u0940",
           "status": "\u0938\u094D\u091F\u0947\u091F\u0938", "premium": "\u092A\u094D\u0930\u0940\u092E\u093F\u092F\u092E \u2B50", "normal": "\u0938\u093E\u092E\u093E\u0928\u094D\u092F",
           "valid": "\u092A\u094D\u0930\u0940\u092E\u093F\u092F\u092E \u0935\u0948\u0927 \u0924\u0915", "expired": "\u092A\u094D\u0930\u0940\u092E\u093F\u092F\u092E \u0938\u092E\u093E\u092A\u094D\u0924",
           "credit": "\u0915\u094D\u0930\u0947\u0921\u093F\u091F", "language": "\u092D\u093E\u0937\u093E",
           "no_premium": "\u092A\u094D\u0930\u0940\u092E\u093F\u092F\u092E: \u0938\u0915\u094D\u0930\u093F\u092F \u0928\u0939\u0940\u0902"},
    "ar": {"title": "\u0627\u0644\u062D\u0633\u0627\u0628", "name": "\u0627\u0644\u0627\u0633\u0645", "username": "\u0627\u0633\u0645 \u0627\u0644\u0645\u0633\u062A\u062E\u062F\u0645", "user_id": "\u0645\u0639\u0631\u0641 \u0627\u0644\u0645\u0633\u062A\u062E\u062F\u0645",
           "status": "\u0627\u0644\u062D\u0627\u0644\u0629", "premium": "\u0628\u0631\u064A\u0645\u064A\u0648\u0645 \u2B50", "normal": "\u0639\u0627\u062F\u064A",
           "valid": "\u0627\u0644\u0628\u0631\u064A\u0645\u064A\u0648\u0645 \u0635\u0627\u0644\u062D \u062D\u062A\u0649", "expired": "\u0627\u0646\u062A\u0647\u0649 \u0627\u0644\u0628\u0631\u064A\u0645\u064A\u0648\u0645 \u0641\u064A",
           "credit": "\u0627\u0644\u0631\u0635\u064A\u062F", "language": "\u0627\u0644\u0644\u063A\u0629",
           "no_premium": "\u0627\u0644\u0628\u0631\u064A\u0645\u064A\u0648\u0645: \u063A\u064A\u0631 \u0645\u0641\u0639\u0644"},
}


def tr_label(text):
    """Translate a single button label to the active language."""
    lang = CURRENT_LANG.get()
    if not text or lang == DEFAULT_LANG:
        return text
    if text in LABELS:
        return LABELS[text].get(lang, text)
    if text.startswith(LOCK + " "):
        inner = text[len(LOCK) + 1:]
        return LOCK + " " + tr_label(inner)
    return text


def L(text):
    """Translate a full reply/button string to the active language."""
    if not text:
        return text
    lang = CURRENT_LANG.get()
    if lang == DEFAULT_LANG:
        return text
    if text in TEXTS:
        return TEXTS[text].get(lang, text)
    return tr_label(text)


def unL(text):
    """Map a translated button label back to its canonical English form."""
    raw = (text or "").strip()
    if not raw:
        return raw
    locked = raw.startswith(LOCK + " ")
    body = raw[len(LOCK) + 1:] if locked else raw
    for english, table in LABELS.items():
        for translated in table.values():
            if translated == body:
                body = english
                break
        else:
            continue
        break
    return (LOCK + " " + body) if locked else body


# Dynamic replies: (regex, {lang: template}) — templates use \\1..\\9 groups.
PATTERN_TEXTS = [
    (re.compile(r"^\U0001F4C4 Page (\d+) \u00B7 Total: (\d+)$"), {
        "bn": "\U0001F4C4 \u09AA\u09C7\u099C {0} \u00B7 \u09AE\u09CB\u099F: {1}",
        "hi": "\U0001F4C4 \u092A\u0947\u091C {0} \u00B7 \u0915\u0941\u0932: {1}",
        "ar": "\U0001F4C4 \u0635\u0641\u062D\u0629 {0} \u00B7 \u0627\u0644\u0625\u062C\u0645\u0627\u0644\u064A: {1}",
    }),
    (re.compile(r"^(\d+)/(\d+) image\(s\) added\. Choose an option\.$"), {
        "bn": "{0}/{1} \u0987\u09AE\u09C7\u099C \u09AF\u09CB\u0997 \u09B9\u09AF\u09BC\u09C7\u099B\u09C7\u0964 \u098F\u0995\u099F\u09BF \u0985\u09AA\u09B6\u09A8 \u09AC\u09C7\u099B\u09C7 \u09A8\u09BF\u09A8\u0964",
        "hi": "{0}/{1} \u0907\u092E\u0947\u091C \u091C\u094B\u0921\u093C\u0947 \u0917\u090F\u0964 \u090F\u0915 \u0935\u093F\u0915\u0932\u094D\u092A \u091A\u0941\u0928\u0947\u0902\u0964",
        "ar": "\u062A\u0645\u062A \u0625\u0636\u0627\u0641\u0629 {0}/{1} \u0635\u0648\u0631\u0629. \u0627\u062E\u062A\u0631 \u062E\u064A\u0627\u0631\u064B\u0627.",
    }),
    (re.compile(r"^(\d+) button\(s\) added\. Choose an option\.$"), {
        "bn": "{0} \u09AC\u09BE\u099F\u09A8 \u09AF\u09CB\u0997 \u09B9\u09AF\u09BC\u09C7\u099B\u09C7\u0964 \u098F\u0995\u099F\u09BF \u0985\u09AA\u09B6\u09A8 \u09AC\u09C7\u099B\u09C7 \u09A8\u09BF\u09A8\u0964",
        "hi": "{0} \u092C\u091F\u0928 \u091C\u094B\u0921\u093C\u0947 \u0917\u090F\u0964 \u090F\u0915 \u0935\u093F\u0915\u0932\u094D\u092A \u091A\u0941\u0928\u0947\u0902\u0964",
        "ar": "\u062A\u0645\u062A \u0625\u0636\u0627\u0641\u0629 {0} \u0623\u0632\u0631\u0627\u0631. \u0627\u062E\u062A\u0631 \u062E\u064A\u0627\u0631\u064B\u0627.",
    }),
    (re.compile(r"^You can add up to (\d+) buttons\.$"), {
        "bn": "আপনি সর্বোচ্চ {0}টি বাটন যোগ করতে পারবেন।",
        "hi": "आप अधिकतम {0} बटन जोड़ सकते हैं।",
        "ar": "يمكنك إضافة حتى {0} أزرار.",
    }),
    (re.compile(r"^\u2B50 Added (\d+) emoji ID\(s\) to your favorites\.$"), {
        "bn": "\u2B50 \u09AB\u09CD\u09AF\u09BE\u09AD\u09B0\u09BF\u099F\u09C7 {0} \u0987\u09AE\u09CB\u099C\u09BF \u0986\u0987\u09A1\u09BF \u09AF\u09CB\u0997 \u09B9\u09AF\u09BC\u09C7\u099B\u09C7\u0964",
        "hi": "\u2B50 \u092A\u0938\u0902\u0926\u0940\u0926\u093E \u092E\u0947\u0902 {0} \u0907\u092E\u094B\u091C\u0940 \u0906\u0908\u0921\u0940 \u091C\u094B\u0921\u093C\u0940 \u0917\u0908\u0902\u0964",
    }),
    (re.compile(r"^\U0001F4E6 (.+)\nTotal emojis: (\d+)\nAdded to database: (\d+)\nChecked: (\d+)/(\d+)\n(.+) Checking\u2026\nEstimated time remaining: (\d+) seconds$"), {
        "bn": "\U0001F4E6 {0}\nমোট ইমোজি: {1}\nডাটাবেজে যোগ হয়েছে: {2}\nচেক করা হয়েছে: {3}/{4}\n{5} চেক করা হচ্ছে…\nআনুমানিক বাকি সময়: {6} সেকেন্ড",
        "hi": "\U0001F4E6 {0}\nकुल इमोजी: {1}\nडेटाबेस में जोड़े गए: {2}\nजाँचे गए: {3}/{4}\n{5} जाँच हो रही है…\nअनुमानित शेष समय: {6} सेकंड",
        "ar": "\U0001F4E6 {0}\nإجمالي الإيموجي: {1}\nتمت الإضافة إلى قاعدة البيانات: {2}\nتم التحقق: {3}/{4}\n{5} جارٍ التحقق…\nالوقت المتبقي المتوقع: {6} ثانية",
    }),
    (re.compile(r"^\u2705 Emoji pack check completed: (.+)\n\n\U0001F4E6 Total emojis: (\d+)\n\U0001F4BE Newly saved: (\d+)\n\u267B\uFE0F Duplicates: (\d+)\n\u26A0\uFE0F Failed to render or validate \(not saved\): (\d+)\n\u23F1\uFE0F Total time: (\d+) seconds$"), {
        "bn": "\u2705 ইমোজি প্যাক চেক সম্পন্ন: {0}\n\n\U0001F4E6 মোট ইমোজি: {1}\n\U0001F4BE নতুন সেভ হয়েছে: {2}\n\u267B\uFE0F ডুপ্লিকেট: {3}\n\u26A0\uFE0F রেন্ডার/যাচাই ব্যর্থ (সেভ হয়নি): {4}\n\u23F1\uFE0F মোট সময়: {5} সেকেন্ড",
        "hi": "\u2705 इमोजी पैक जाँच पूर्ण: {0}\n\n\U0001F4E6 कुल इमोजी: {1}\n\U0001F4BE नए सहेजे गए: {2}\n\u267B\uFE0F डुप्लिकेट: {3}\n\u26A0\uFE0F रेंडर/सत्यापन विफल (सहेजे नहीं गए): {4}\n\u23F1\uFE0F कुल समय: {5} सेकंड",
        "ar": "\u2705 اكتمل فحص حزمة الإيموجي: {0}\n\n\U0001F4E6 إجمالي الإيموجي: {1}\n\U0001F4BE تم حفظ الجديد: {2}\n\u267B\uFE0F المكرر: {3}\n\u26A0\uFE0F فشل العرض/التحقق (لم يُحفظ): {4}\n\u23F1\uFE0F الوقت الإجمالي: {5} ثانية",
    }),
]


def translate_text(text):
    """Translate a reply to the active language (best effort, line by line)."""
    lang = CURRENT_LANG.get()
    if not text or lang == DEFAULT_LANG or not isinstance(text, str):
        return text
    if text in TEXTS:
        return TEXTS[text].get(lang, text)
    if text in LABELS:
        return LABELS[text].get(lang, text)
    if text.startswith(LOCK + " "):
        inner = translate_text(text[len(LOCK) + 1:])
        return LOCK + " " + inner
    # Whole-message dynamic patterns (may span several lines).
    for pattern, table in PATTERN_TEXTS:
        match = pattern.match(text)
        if match:
            return table[lang].format(*match.groups())
    # Multi-line messages: translate each line that is known.
    if "\n" in text:
        lines = text.split("\n")
        changed = False
        out = []
        for line in lines:
            stripped = line.strip()
            new_line = line
            if stripped in TEXTS:
                new_line = line.replace(stripped, TEXTS[stripped].get(lang, stripped))
                changed = True
            elif stripped in LABELS:
                new_line = line.replace(stripped, LABELS[stripped].get(lang, stripped))
                changed = True
            else:
                for pattern, table in PATTERN_TEXTS:
                    match = pattern.match(stripped)
                    if match:
                        new_line = line.replace(stripped, table[lang].format(*match.groups()))
                        changed = True
                        break
            out.append(new_line)
        return "\n".join(out) if changed else text
    return text


def localize_markup(markup):
    """Translate every button label inside a reply/inline markup (dict form)."""
    if not isinstance(markup, dict):
        return markup
    result = dict(markup)
    for key in ("keyboard", "inline_keyboard"):
        if key in result and isinstance(result[key], list):
            rows = []
            for row in result[key]:
                new_row = []
                for button in row:
                    if isinstance(button, dict):
                        item = dict(button)
                        if "text" in item:
                            item["text"] = translate_text(str(item["text"]))
                        new_row.append(item)
                    else:
                        new_row.append(button)
                rows.append(new_row)
            result[key] = rows
    return result


def install_outgoing_localizer(app):
    """Wrap Bot send/edit methods so every outgoing text/button is localised."""
    from telegram import Bot as _Bot
    if getattr(_Bot, "_peb_localized", False):
        return

    def _wrap(original, text_index, text_key):
        async def wrapper(self, *args, **kwargs):
            # Never rewrite user-formatted content that carries entities,
            # because translating it would invalidate the entity offsets.
            if kwargs.get("entities") or kwargs.get("caption_entities"):
                return await original(self, *args, **kwargs)
            try:
                if text_key in kwargs:
                    kwargs[text_key] = translate_text(kwargs[text_key])
                elif len(args) > text_index and isinstance(args[text_index], str):
                    args = list(args)
                    args[text_index] = translate_text(args[text_index])
                if "reply_markup" in kwargs:
                    kwargs["reply_markup"] = localize_markup(kwargs["reply_markup"])
            except Exception:
                pass
            return await original(self, *args, **kwargs)
        return wrapper

    _Bot.send_message = _wrap(_Bot.send_message, 1, "text")
    _Bot.edit_message_text = _wrap(_Bot.edit_message_text, 0, "text")
    if hasattr(_Bot, "send_photo"):
        _Bot.send_photo = _wrap(_Bot.send_photo, 2, "caption")
    if hasattr(_Bot, "edit_message_caption"):
        _Bot.edit_message_caption = _wrap(_Bot.edit_message_caption, 0, "caption")
    _Bot._peb_localized = True


def language_keyboard():
    rows = [[KeyboardButton(LANG_NAMES[code])] for code in LANG_ORDER]
    rows.append([KeyboardButton(L("Cancel"))])
    return green_reply_markup(rows)


def set_user_lang(user_id, lang):
    if lang not in LANG_NAMES:
        return
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))
        conn.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, int(user_id)))


def get_user_lang(user_id):
    with db() as conn:
        row = conn.execute("SELECT lang FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    return (row["lang"] if row and row["lang"] else "") if row else ""


def color_style(color):
    return COLOR_STYLES.get(str(color or "normal").strip().lower())


def get_menu_buttons(scope="main"):
    with db() as conn:
        rows = conn.execute(
            "SELECT btn_id, key, text, emoji_id, color, order_idx, scope "
            "FROM menu_buttons WHERE scope=? ORDER BY order_idx, btn_id", (scope,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_menu_button(btn_id):
    with db() as conn:
        row = conn.execute(
            "SELECT btn_id, key, text, emoji_id, color, order_idx, scope "
            "FROM menu_buttons WHERE btn_id=?", (int(btn_id),)
        ).fetchone()
    return dict(row) if row else None


def menu_button_by_text(text, scope="main"):
    with db() as conn:
        row = conn.execute(
            "SELECT btn_id, key, text, emoji_id, color, order_idx, scope "
            "FROM menu_buttons WHERE text=? AND scope=?",
            ((text or "").strip(), scope)
        ).fetchone()
    return dict(row) if row else None


def registry_row(scope, key):
    with db() as conn:
        row = conn.execute(
            "SELECT btn_id, key, text, emoji_id, color, order_idx, scope "
            "FROM menu_buttons WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
    return dict(row) if row else None


def ensure_registry_buttons(scope, items):
    """Insert any missing registry rows for a scope (existing rows are kept)."""
    with db() as conn:
        existing = {r["key"] for r in conn.execute(
            "SELECT key FROM menu_buttons WHERE scope=?", (scope,)).fetchall()}
        max_id = conn.execute("SELECT COALESCE(MAX(btn_id),0) FROM menu_buttons").fetchone()[0]
        order = conn.execute(
            "SELECT COALESCE(MAX(order_idx),0) FROM menu_buttons WHERE scope=?", (scope,)
        ).fetchone()[0]
        for key, text in items:
            if key in existing:
                continue
            max_id += 1
            order += 1
            conn.execute(
                "INSERT INTO menu_buttons(btn_id, key, text, emoji_id, color, order_idx, scope) "
                "VALUES(?,?,?,?,?,?,?)",
                (max_id, key, text, "", "green", order, scope),
            )


def sync_category_buttons():
    """Make sure every category has an editable button in the category menus."""
    names = category_names()
    items = [(f"cat:{name}", name) for name in names]
    ensure_registry_buttons("category", items)
    ensure_registry_buttons("admin_categories", items)


def scoped_keyboard(scope, layout, resize_keyboard=True):
    """Build a reply keyboard from the registry.

    layout: list of rows, each row is a list of (key, default_text) pairs.
    Text, custom emoji and colour are read from the registry so /cng_emoji,
    /cng_calel and /edit_button work on every menu.
    """
    rows = []
    for row in layout:
        built = []
        for key, default in row:
            reg = registry_row(scope, key)
            text = reg["text"] if reg and reg.get("text") else default
            item = {"text": tr_label(text)}
            if reg and reg.get("emoji_id"):
                item["icon_custom_emoji_id"] = str(reg["emoji_id"])
            style = color_style(reg.get("color")) if reg else "success"
            if style:
                item["style"] = style
            built.append(item)
        if built:
            rows.append(built)
    return {"keyboard": rows, "resize_keyboard": resize_keyboard}


def main_menu(user_id=None):
    """Build the main reply keyboard from the database registry."""
    buttons = get_menu_buttons("main")
    if not is_admin(user_id or 0):
        buttons = [b for b in buttons if b["key"] != "order"]
    rows = []
    for i in range(0, len(buttons), 2):
        row = []
        for button in buttons[i:i + 2]:
            item = {"text": tr_label(button["text"])}
            if button.get("emoji_id"):
                item["icon_custom_emoji_id"] = str(button["emoji_id"])
            style = color_style(button.get("color"))
            if style:
                item["style"] = style
            row.append(item)
        rows.append(row)
    return {"keyboard": rows, "resize_keyboard": True}


def main_menu_for(update, context=None):
    """Main menu tailored to the person who is talking to the bot."""
    uid = None
    try:
        uid = update.effective_user.id
    except Exception:
        uid = None
    if uid is None and context is not None:
        uid = context.user_data.get("uid")
    return main_menu(uid)
HELP_TEXT = (
    "Usage:\n"
    "• Add emoji: Send an emoji pack link, a message/forward containing Premium Emoji, or an ID.\n"
    "• Remove emoji: Send one or more IDs (one per line is supported).\n"
    "• Make a post: Use a format like `Hello {5942533496823093848}`.\n"
    "• View emoji ID: Send a pack link, an ID, or a message containing Premium Emoji.\n"
    "• Account / Refer / Task / Shop / Redeem code / Tutorial / Support: use the menu buttons.\n"
    "• /id: show the ID of any menu button.\n"
    "• Admin: /cng_emoji <button_id> <emoji_id>, /cng_calel <button_id> <red|blue|green|normal>, "
    "/cng_order <button_id> <order>.\n"
    "• Standard Unicode emoji do not have Custom Emoji IDs."
)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS emojis (
            emoji_id TEXT PRIMARY KEY, alt TEXT NOT NULL DEFAULT '😀', pack TEXT DEFAULT '',
            is_favorite INTEGER NOT NULL DEFAULT 0, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS emoji_categories (emoji_id TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(emoji_id, category))")
        # Bot users (for the web dashboard statistics).
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Channels that a user must join before using the bot.
        conn.execute("""CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT DEFAULT '',
            username TEXT DEFAULT '',
            title TEXT DEFAULT '',
            invite_link TEXT DEFAULT '',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Main-menu button registry (stable IDs, emoji, colour, order).
        conn.execute("""CREATE TABLE IF NOT EXISTS menu_buttons (
            btn_id INTEGER PRIMARY KEY,
            key TEXT NOT NULL,
            text TEXT NOT NULL,
            emoji_id TEXT DEFAULT '',
            color TEXT NOT NULL DEFAULT 'green',
            order_idx INTEGER NOT NULL DEFAULT 0,
            scope TEXT NOT NULL DEFAULT 'main')""")
        # Older databases had UNIQUE(key); rebuild so every menu can reuse keys.
        mb_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='menu_buttons'"
        ).fetchone()
        if mb_sql and "key TEXT UNIQUE" in (mb_sql[0] or ""):
            conn.execute("ALTER TABLE menu_buttons RENAME TO menu_buttons_old")
            conn.execute("""CREATE TABLE menu_buttons (
                btn_id INTEGER PRIMARY KEY,
                key TEXT NOT NULL,
                text TEXT NOT NULL,
                emoji_id TEXT DEFAULT '',
                color TEXT NOT NULL DEFAULT 'green',
                order_idx INTEGER NOT NULL DEFAULT 0,
                scope TEXT NOT NULL DEFAULT 'main')""")
            conn.execute(
                "INSERT INTO menu_buttons(btn_id, key, text, emoji_id, color, order_idx, scope) "
                "SELECT btn_id, key, text, emoji_id, color, order_idx, 'main' FROM menu_buttons_old"
            )
            conn.execute("DROP TABLE menu_buttons_old")
        # Per-user channels that the user can publish posts to.
        conn.execute("""CREATE TABLE IF NOT EXISTS user_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id TEXT DEFAULT '',
            username TEXT DEFAULT '',
            title TEXT DEFAULT '',
            invite_link TEXT DEFAULT '',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, chat_id, username))""")
        # Per-user favorites (each user keeps an independent list).
        conn.execute("""CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER NOT NULL,
            emoji_id TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, emoji_id))""")
        # Redeem codes created by the owner.
        conn.execute("""CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            reward INTEGER NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Which user already redeemed which code.
        conn.execute("""CREATE TABLE IF NOT EXISTS redeem_log (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, code))""")
        # Daily task claims.
        conn.execute("""CREATE TABLE IF NOT EXISTS task_claims (
            user_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, task))""")
        # Referral records.
        conn.execute("""CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Shop packages (editable from the web dashboard).
        conn.execute("""CREATE TABLE IF NOT EXISTS shop_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            price INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1)""")
        # Orders placed through the shop.
        conn.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            package_id INTEGER DEFAULT 0,
            package_label TEXT DEFAULT '',
            amount INTEGER NOT NULL DEFAULT 0,
            price INTEGER NOT NULL DEFAULT 0,
            method TEXT DEFAULT '',
            trx TEXT DEFAULT '',
            screenshot_file_id TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Premium orders are activated by the user, not by the approval itself.
        order_cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if 'activated' not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN activated INTEGER NOT NULL DEFAULT 0")
        # Key/value settings (payment numbers, contact links, etc.).
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '')""")
        # Seed default shop packages once.
        if not conn.execute("SELECT 1 FROM shop_packages LIMIT 1").fetchone():
            seed_packages = [
                ("credit", "25 credit", 25, 19),
                ("credit", "50 credit", 50, 35),
                ("credit", "100 credit", 100, 49),
                ("premium", "7 days", 7, 19),
                ("premium", "15 days", 15, 29),
                ("premium", "30 days", 30, 49),
                ("ff", "1k like", 1000, 40),
                ("ff", "3k like", 3000, 100),
                ("ff", "6k like", 6000, 180),
            ]
            for i, (kind, label, amount, price) in enumerate(seed_packages, 1):
                conn.execute(
                    "INSERT INTO shop_packages(kind, label, amount, price, sort_order) "
                    "VALUES(?,?,?,?,?)",
                    (kind, label, amount, price, i),
                )
        # Seed default settings once.
        default_settings = {
            "bikash_number": "01616948550",
            "nagad_number": "01889617087",
            "whatsapp_link": "https://wa.me/8801616948550",
            "telegram_link": "https://t.me/",
            "redeem_fee": "1",
            "web_base_url": "",
            "block_vpn": "1",
        }
        for key, value in default_settings.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        # Add stable serial/order columns to existing databases without losing data.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(emojis)")}
        if 'sort_order' not in cols:
            conn.execute("ALTER TABLE emojis ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            rows = conn.execute("SELECT emoji_id FROM emojis ORDER BY added_at, emoji_id").fetchall()
            for i, row in enumerate(rows, 1): conn.execute("UPDATE emojis SET sort_order=? WHERE emoji_id=?", (i, row[0]))
        cat_cols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
        if 'category_id' not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN category_id INTEGER")
        cat_cols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
        if 'display_order' not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
        rows = conn.execute("SELECT name FROM categories WHERE category_id IS NULL OR category_id=0 ORDER BY name COLLATE NOCASE").fetchall()
        next_id = conn.execute("SELECT COALESCE(MAX(category_id),0)+1 FROM categories").fetchone()[0]
        for i, row in enumerate(rows): conn.execute("UPDATE categories SET category_id=? WHERE name=?", (next_id+i, row[0]))
        rows = conn.execute("SELECT name FROM categories WHERE display_order IS NULL OR display_order=0 ORDER BY category_id").fetchall()
        next_order = conn.execute("SELECT COALESCE(MAX(display_order),0)+1 FROM categories").fetchone()[0]
        for i, row in enumerate(rows): conn.execute("UPDATE categories SET display_order=? WHERE name=?", (next_order+i, row[0]))
        ec_cols = {r[1] for r in conn.execute("PRAGMA table_info(emoji_categories)")}
        if 'sort_order' not in ec_cols:
            conn.execute("ALTER TABLE emoji_categories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            rows = conn.execute("SELECT category, emoji_id FROM emoji_categories ORDER BY category, rowid").fetchall()
            counters = {}
            for row in rows:
                counters[row['category']] = counters.get(row['category'], 0) + 1
                conn.execute("UPDATE emoji_categories SET sort_order=? WHERE category=? AND emoji_id=?", (counters[row['category']], row['category'], row['emoji_id']))
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if 'balance' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
        if 'referred_by' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT 0")
        if 'premium_until' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN premium_until TEXT DEFAULT ''")
        if 'lang' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT ''")
        if 'onboarded' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
        if 'premium_claimed' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN premium_claimed INTEGER NOT NULL DEFAULT 0")
        # One free premium per device: the IP and the browser/device fingerprint
        # are stored (hashed) so a second claim from the same device is refused.
        conn.execute("""CREATE TABLE IF NOT EXISTS premium_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            ip_hash TEXT NOT NULL DEFAULT '',
            device_hash TEXT NOT NULL DEFAULT '',
            claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Every menu gets its own editable buttons (scope column).
        mb_cols = {r[1] for r in conn.execute("PRAGMA table_info(menu_buttons)")}
        if 'scope' not in mb_cols:
            conn.execute("ALTER TABLE menu_buttons ADD COLUMN scope TEXT NOT NULL DEFAULT 'main'")
        # Seed the main-menu button registry (keyed by `key`, so an existing
        # database keeps its customised labels/emoji and still receives new
        # buttons such as "Language"). If a preferred btn_id is already taken
        # by another menu, a fresh id is allocated instead of silently skipping.
        existing_buttons = {r[0] for r in conn.execute("SELECT btn_id FROM menu_buttons")}
        existing_main_keys = {r[0] for r in conn.execute(
            "SELECT key FROM menu_buttons WHERE scope='main'").fetchall()}
        for order_idx, (btn_id, key, text, emoji_id, color) in enumerate(DEFAULT_MENU_BUTTONS, 1):
            if key in existing_main_keys:
                continue
            target_id = btn_id
            if target_id in existing_buttons:
                target_id = conn.execute(
                    "SELECT COALESCE(MAX(btn_id),0)+1 FROM menu_buttons").fetchone()[0]
            conn.execute(
                "INSERT INTO menu_buttons(btn_id, key, text, emoji_id, color, order_idx, scope) "
                "VALUES(?,?,?,?,?,?,'main')",
                (target_id, key, text, emoji_id, color, order_idx),
            )
            existing_buttons.add(target_id)
            existing_main_keys.add(key)
        for scope, items in DEFAULT_SCOPED_BUTTONS.items():
            existing = {r[0] for r in conn.execute(
                "SELECT key FROM menu_buttons WHERE scope=?", (scope,)).fetchall()}
            max_id = conn.execute("SELECT COALESCE(MAX(btn_id),0) FROM menu_buttons").fetchone()[0]
            order = conn.execute(
                "SELECT COALESCE(MAX(order_idx),0) FROM menu_buttons WHERE scope=?", (scope,)
            ).fetchone()[0]
            for key, text in items:
                if key in existing:
                    continue
                max_id += 1
                order += 1
                conn.execute(
                    "INSERT INTO menu_buttons(btn_id, key, text, emoji_id, color, order_idx, scope) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (max_id, key, text, "", "green", order, scope),
                )
        # Category buttons live in the registry too, so they can be edited.
        for scope in ("category", "admin_categories"):
            existing = {r[0] for r in conn.execute(
                "SELECT key FROM menu_buttons WHERE scope=?", (scope,)).fetchall()}
            max_id = conn.execute("SELECT COALESCE(MAX(btn_id),0) FROM menu_buttons").fetchone()[0]
            order = conn.execute(
                "SELECT COALESCE(MAX(order_idx),0) FROM menu_buttons WHERE scope=?", (scope,)
            ).fetchone()[0]
            for cat in conn.execute("SELECT name FROM categories ORDER BY display_order, category_id").fetchall():
                key = f"cat:{cat[0]}"
                if key in existing:
                    continue
                max_id += 1
                order += 1
                conn.execute(
                    "INSERT INTO menu_buttons(btn_id, key, text, emoji_id, color, order_idx, scope) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (max_id, key, cat[0], "", "green", order, scope),
                )


def save_emoji(emoji_id, alt="😀", pack="", preserve_order=None):
    """Insert only new IDs. Never count or overwrite an existing ID as added."""
    emoji_id = str(emoji_id).strip()
    if not re.fullmatch(r"\d{5,30}", emoji_id):
        return False
    with db() as conn:
        if preserve_order is not None:
            conn.execute("INSERT INTO emojis(emoji_id, alt, pack, sort_order) VALUES(?, ?, ?, ?) ON CONFLICT(emoji_id) DO UPDATE SET alt=excluded.alt, pack=excluded.pack, sort_order=excluded.sort_order",
                         (emoji_id, alt or "😀", pack or "", int(preserve_order)))
            return True
        next_order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM emojis").fetchone()[0]
        cur = conn.execute(
            "INSERT OR IGNORE INTO emojis(emoji_id, alt, pack, sort_order) VALUES(?, ?, ?, ?)",
            (emoji_id, alt or "😀", pack or "", next_order)
        )
        return cur.rowcount == 1


def remove_emoji_ids(ids):
    with db() as conn:
        cur = conn.executemany("DELETE FROM emojis WHERE emoji_id=?", [(str(i),) for i in ids])
        return cur.rowcount


def get_emojis(favorites=False, user_id=None):
    with db() as conn:
        if favorites:
            if user_id is None:
                rows = conn.execute(
                    "SELECT emoji_id, alt FROM emojis WHERE is_favorite=1 ORDER BY sort_order, added_at, emoji_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT e.emoji_id, e.alt FROM emojis e
                       JOIN user_favorites f ON f.emoji_id = e.emoji_id
                       WHERE f.user_id = ?
                       ORDER BY f.added_at, e.sort_order, e.emoji_id""",
                    (int(user_id),),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT emoji_id, alt FROM emojis ORDER BY sort_order, added_at, emoji_id"
            ).fetchall()
    return [dict(row) for row in rows]


def set_favorites(ids, enabled, user_id=None):
    """Add or remove favorites for one user (per-user lists)."""
    with db() as conn:
        found = []
        for emoji_id in ids:
            emoji_id = str(emoji_id)
            exists = conn.execute("SELECT 1 FROM emojis WHERE emoji_id=?", (emoji_id,)).fetchone()
            if not exists:
                continue
            if user_id is None:
                conn.execute("UPDATE emojis SET is_favorite=? WHERE emoji_id=?", (1 if enabled else 0, emoji_id))
            elif enabled:
                conn.execute("INSERT OR IGNORE INTO user_favorites(user_id, emoji_id) VALUES(?, ?)", (int(user_id), emoji_id))
            else:
                conn.execute("DELETE FROM user_favorites WHERE user_id=? AND emoji_id=?", (int(user_id), emoji_id))
            found.append(emoji_id)
        return found


def user_balance(user_id):
    with db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    return int(row["balance"]) if row else 0


def add_balance(user_id, amount):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (int(amount), int(user_id)))
    return user_balance(user_id)


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_packages(kind):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM shop_packages WHERE kind=? AND active=1 ORDER BY sort_order, id",
            (kind,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_package(package_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM shop_packages WHERE id=?", (int(package_id),)).fetchone()
    return dict(row) if row else None


def reorder_menu_button(btn_id, position):
    """Move a menu button to an exact 1-based position inside its own menu."""
    btn_id = int(btn_id)
    btn = get_menu_button(btn_id)
    if not btn:
        return False
    scope = btn.get("scope") or "main"
    buttons = get_menu_buttons(scope)
    ids = [b["btn_id"] for b in buttons]
    if btn_id not in ids:
        return False
    ids.remove(btn_id)
    position = max(1, min(int(position), len(ids) + 1))
    ids.insert(position - 1, btn_id)
    with db() as conn:
        for index, bid in enumerate(ids, 1):
            conn.execute("UPDATE menu_buttons SET order_idx=? WHERE btn_id=?", (index, bid))
    return True


def move_menu_button(btn_id, direction):
    """Move a button one step left/right (1) or up/down (2) in the 2-column menu."""
    btn = get_menu_button(int(btn_id))
    if not btn:
        return False
    buttons = get_menu_buttons(btn.get("scope") or "main")
    ids = [b["btn_id"] for b in buttons]
    if int(btn_id) not in ids:
        return False
    index = ids.index(int(btn_id))
    step = {"left": -1, "right": 1, "up": -2, "down": 2}.get(direction, 0)
    target = max(0, min(len(ids) - 1, index + step))
    if target == index:
        return False
    return reorder_menu_button(btn_id, target + 1)


def current_menu_context(context):
    """Infer which reply keyboard the user is currently looking at."""
    mode = context.user_data.get("mode")
    if mode in (None, ""):
        return "main"
    if mode == "make_post":
        return "favorites" if context.user_data.get("showing_favorites") else "category"
    if mode == "bot_settings":
        return "bot_settings"
    if mode == "admin_categories":
        return "admin_categories"
    if mode == "admin_category_page":
        return "admin_category_page"
    if mode == "shop_payment":
        return "payment"
    if mode in ("shop", "shop_pick_credit", "shop_pick_premium", "shop_pick_ff", "shop_trx"):
        return "shop"
    if mode == "refer":
        return "refer"
    if mode == "task":
        return "task"
    if context.user_data.get("current_category"):
        return "admin_category_page"
    return "main"


def menu_markup_for_context(context, menu_context=None):
    """Rebuild the reply keyboard for a menu context."""
    menu_context = menu_context or current_menu_context(context)
    if menu_context == "category":
        return emoji_category_keyboard()
    if menu_context == "favorites":
        return favorite_keyboard()
    if menu_context == "bot_settings":
        return bot_settings_keyboard()
    if menu_context == "admin_categories":
        return admin_categories_keyboard()
    if menu_context == "admin_category_page":
        return admin_category_page_keyboard()
    if menu_context == "shop":
        return shop_menu_markup()
    if menu_context == "payment":
        return payment_method_markup(context.user_data.get("payment_selected"))
    if menu_context == "refer":
        return refer_menu_markup()
    if menu_context == "task":
        return task_menu_markup()
    return main_menu(context.user_data.get("uid"))


def resolve_button_identifier(label, menu_context):
    """Return (id_string, name) for a tapped button in a given menu context."""
    label = (label or "").strip()
    scope = menu_context or "main"
    btn = menu_button_by_text(label, scope)
    if btn:
        return str(btn["btn_id"]), btn["text"]
    # A selected payment method is shown with a tick in front of its label.
    stripped = label.replace("\u2705", "").strip()
    if stripped != label:
        btn = menu_button_by_text(stripped, scope)
        if btn:
            return str(btn["btn_id"]), btn["text"]
    btn = menu_button_by_text(label, "main")
    if btn:
        return str(btn["btn_id"]), btn["text"]
    with db() as conn:
        row = conn.execute("SELECT category_id FROM categories WHERE name=?", (label,)).fetchone()
    if row:
        return f"cat:{row['category_id']}", label
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "button"
    return f"menu:{slug}", label


def is_premium(user_id):
    with db() as conn:
        row = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    if not row or not row["premium_until"]:
        return False
    try:
        until = datetime.datetime.fromisoformat(str(row["premium_until"]))
    except ValueError:
        return False
    return until > datetime.datetime.utcnow()


def premium_until_text(user_id):
    """Return a friendly string for the user's premium expiry."""
    with db() as conn:
        row = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    raw = (row["premium_until"] if row else "") or ""
    if not raw:
        return "\u2014"
    try:
        until = datetime.datetime.fromisoformat(str(raw))
    except ValueError:
        return "\u2014"
    return until.strftime("%d %b %Y, %H:%M UTC")


def grant_premium(user_id, days):
    now = datetime.datetime.utcnow()
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))
        row = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        base = now
        if row and row["premium_until"]:
            try:
                existing = datetime.datetime.fromisoformat(str(row["premium_until"]))
                if existing > now:
                    base = existing
            except ValueError:
                pass
        until = base + datetime.timedelta(days=int(days))
        conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until.isoformat(), int(user_id)))
    return until


def create_redeem_code(reward, max_uses, created_by):
    code = secrets.token_hex(4).upper()
    with db() as conn:
        while conn.execute("SELECT 1 FROM redeem_codes WHERE code=?", (code,)).fetchone():
            code = secrets.token_hex(4).upper()
        conn.execute(
            "INSERT INTO redeem_codes(code, reward, max_uses, created_by) VALUES(?,?,?,?)",
            (code, int(reward), int(max_uses), int(created_by)),
        )
    return code


def create_order(user_id, kind, package, method, trx="", screenshot_file_id=""):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders(user_id, kind, package_id, package_label, amount, price, "
            "method, trx, screenshot_file_id, status) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
            (
                int(user_id), kind, int(package.get("id", 0)), package.get("label", ""),
                int(package.get("amount", 0)), int(package.get("price", 0)),
                method, trx or "", screenshot_file_id or "",
            ),
        )
        return cur.lastrowid


def get_orders(status=None, limit=100):
    query = "SELECT * FROM orders"
    params = []
    if status:
        query += " WHERE status=?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def approve_order(order_id):
    """Mark an order approved and apply its benefit. Returns the order dict."""
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (int(order_id),)).fetchone()
        if not order:
            return None
        order = dict(order)
        if order["status"] == "approved":
            return order
        conn.execute(
            "UPDATE orders SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(order_id),),
        )
        order["status"] = "approved"
    if order["kind"] == "credit":
        add_balance(order["user_id"], int(order["amount"]))
    # Premium is NOT granted here: the user activates it with the
    # "Active premium now" button in the confirmation message.
    return order


def activate_premium_order(order_id):
    """Activate a premium order the user confirmed. Returns (order, status)."""
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (int(order_id),)).fetchone()
        if not row:
            return None, "notfound"
        order = dict(row)
        if order["kind"] != "premium":
            return order, "notpremium"
        if order["status"] != "approved":
            return order, "notapproved"
        if int(order.get("activated") or 0):
            return order, "already"
        conn.execute(
            "UPDATE orders SET activated=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(order_id),),
        )
    grant_premium(order["user_id"], int(order["amount"]))
    order["activated"] = 1
    return order, "ok"


def reject_order(order_id):
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (int(order_id),)).fetchone()
        if not order:
            return None
        conn.execute(
            "UPDATE orders SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(order_id),),
        )
        order = dict(order)
        order["status"] = "rejected"
    return order


BOT_APP = None
BOT_LOOP = None

ORDER_STATUS_ICON = {"pending": "\u23f3", "approved": "\u2705", "rejected": "\u274c"}
ORDER_KIND_LABEL = {
    "credit": "Credit", "premium": "Premium", "ff": "FF auto like",
}


def get_order(order_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (int(order_id),)).fetchone()
    return dict(row) if row else None


def get_user_orders(user_id, limit=200):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def order_detail_text(order):
    """Human readable order block used by both the user and the admin."""
    status = order.get("status", "pending")
    dash = "\u2014"
    trx = order["trx"] or ("screenshot" if order["screenshot_file_id"] else dash)
    lines = [
        f"\U0001F9FE Order #{order['id']}",
        f"Product: {ORDER_KIND_LABEL.get(order['kind'], order['kind'])}",
        f"Package: {order['package_label']}",
        f"Credits/Likes: {order['amount']}",
        f"Price: {order['price']} taka",
        f"Method: {order['method'] or dash}",
        f"TrxID: {trx}",
        f"Status: {ORDER_STATUS_ICON.get(status, '')} {status.title()}",
        f"Date: {order['created_at']}",
    ]
    return "\n".join(lines)


async def send_order_to_admin(bot, order_id):
    """Notify the admin about a new order with inline approve/reject buttons."""
    if not ADMIN_ID:
        return
    order = get_order(order_id)
    if not order:
        return
    with db() as conn:
        user = conn.execute(
            "SELECT username, first_name FROM users WHERE user_id=?", (order["user_id"],)
        ).fetchone()
    who = ""
    if user:
        who = user["first_name"] or ""
        if user["username"]:
            who += f" (@{user['username']})"
    markup = {"inline_keyboard": [[
        {"text": "\u2705 Approve", "callback_data": f"ord:approve:{order_id}"},
        {"text": "\u274c Reject", "callback_data": f"ord:reject:{order_id}"},
    ]]}
    text = (
        "\U0001F514 New order received\n\n"
        f"User: {who or order['user_id']}\n"
        f"User ID: {order['user_id']}\n\n"
        + order_detail_text(order)
    )
    try:
        await bot.send_message(chat_id=int(ADMIN_ID), text=text, reply_markup=markup)
    except Exception as exc:
        print("send_order_to_admin error:", repr(exc))


def notify_user(user_id, text):
    if BOT_APP is None or BOT_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            BOT_APP.bot.send_message(chat_id=int(user_id), text=text), BOT_LOOP
        )
    except Exception as exc:
        print("notify_user error:", repr(exc))



def notify_user_markup(user_id, text, markup=None):
    if BOT_APP is None or BOT_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            BOT_APP.bot.send_message(chat_id=int(user_id), text=text, reply_markup=markup),
            BOT_LOOP,
        )
    except Exception as exc:
        print("notify_user_markup error:", repr(exc))


def is_admin(user_id):
    return bool(ADMIN_ID) and int(user_id or 0) == int(ADMIN_ID)


def track_user(update):
    """Remember every user that interacts with the bot (dashboard stats)."""
    user = getattr(update, "effective_user", None)
    if not user:
        return
    try:
        with db() as conn:
            conn.execute(
                """INSERT INTO users(user_id, username, first_name, last_seen)
                   VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username=excluded.username,
                       first_name=excluded.first_name,
                       last_seen=CURRENT_TIMESTAMP""",
                (int(user.id), user.username or "", user.first_name or ""),
            )
    except sqlite3.Error as exc:
        print("track_user error:", repr(exc))


def get_channels():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY id").fetchall()]


def channel_target(channel):
    """Return the value accepted by get_chat_member for a stored channel."""
    return (channel.get("chat_id") or "").strip() or (channel.get("username") or "").strip()


def channel_link(channel):
    link = (channel.get("invite_link") or "").strip()
    if link:
        return link
    username = (channel.get("username") or "").strip()
    if username:
        return "https://t.me/" + username.lstrip("@")
    return ""


# ---------------------------------------------------------------------------
# Per-user channels (for the Post channel feature).
# ---------------------------------------------------------------------------
def get_user_channels(user_id):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM user_channels WHERE user_id=? ORDER BY added_at, id",
            (int(user_id),),
        ).fetchall()]


def add_user_channel(user_id, chat_id, username, title, invite_link):
    """Store a channel for a user. Returns (ok, count). Max MAX_USER_CHANNELS."""
    chat_id = (chat_id or "").strip()
    username = (username or "").strip().lstrip("@")
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM user_channels WHERE user_id=?", (int(user_id),)
        ).fetchone()[0]
        if chat_id:
            exists = conn.execute(
                "SELECT 1 FROM user_channels WHERE user_id=? AND chat_id=?",
                (int(user_id), chat_id),
            ).fetchone()
        else:
            exists = conn.execute(
                "SELECT 1 FROM user_channels WHERE user_id=? AND username=?",
                (int(user_id), username),
            ).fetchone()
        if not exists and count >= MAX_USER_CHANNELS:
            return False, count
        if exists:
            conn.execute(
                "UPDATE user_channels SET title=?, invite_link=? WHERE user_id=? AND "
                + ("chat_id=?" if chat_id else "username=?"),
                (title or "", invite_link or "", int(user_id), chat_id or username),
            )
        else:
            conn.execute(
                "INSERT INTO user_channels(user_id, chat_id, username, title, invite_link) "
                "VALUES(?,?,?,?,?)",
                (int(user_id), chat_id, username, title or "", invite_link or ""),
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM user_channels WHERE user_id=?", (int(user_id),)
        ).fetchone()[0]
    return True, count


def remove_user_channel(user_id, ref):
    ref = (ref or "").strip().lstrip("@")
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM user_channels WHERE user_id=? AND (username=? OR chat_id=? OR title=?)",
            (int(user_id), ref, ref, ref),
        )
        return cur.rowcount > 0


def parse_channel_input(text):
    """Parse 'username link id' input into a channel dict."""
    text = text or ""
    username = ""
    chat_id = ""
    link = ""
    match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})", text)
    if match:
        username = match.group(1)
        link = f"https://t.me/{username}"
    at_match = re.search(r"@([A-Za-z0-9_]{4,})", text)
    if at_match and not username:
        username = at_match.group(1)
        link = f"https://t.me/{username}"
    id_match = re.search(r"(?<![\w/])-?\d{5,}(?![\w])", text)
    if id_match:
        chat_id = id_match.group(0)
    if not username and not chat_id:
        return None
    return {
        "username": username,
        "chat_id": chat_id,
        "link": link,
        "title": username or chat_id,
    }


def channel_post_target(channel):
    chat_id = (channel.get("chat_id") or "").strip()
    if chat_id:
        return chat_id
    username = (channel.get("username") or "").strip().lstrip("@")
    return "@" + username if username else ""


async def bot_is_admin(bot, target):
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=target, user_id=me.id)
        return str(getattr(member, "status", "")).lower() in {"administrator", "creator"}
    except TelegramError as exc:
        print("bot_is_admin error:", repr(exc))
        return False


async def user_is_admin(bot, target, user_id):
    try:
        member = await bot.get_chat_member(chat_id=target, user_id=user_id)
        return str(getattr(member, "status", "")).lower() in {"administrator", "creator"}
    except TelegramError as exc:
        print("user_is_admin error:", repr(exc))
        return False


async def missing_channels(bot, user_id):
    """Return channels the user has not joined yet.

    The bot must be an administrator in each channel so Telegram allows the
    getChatMember membership check. If a check fails we treat the channel as
    not joined, which keeps the gate secure instead of silently bypassing it.
    """
    missing = []
    for channel in get_channels():
        target = channel_target(channel)
        if not target:
            continue
        try:
            member = await bot.get_chat_member(chat_id=target, user_id=user_id)
            status = str(getattr(member, "status", "")).lower()
            if status in {"left", "kicked"}:
                missing.append(channel)
        except TelegramError as exc:
            print(f"Membership check failed for {target}: {exc!r}")
            missing.append(channel)
    return missing


JOIN_INTRO = {
    "en": "\U0001F512 To use this bot you must first join the channel(s) below:",
    "bn": "\U0001F512 \u09AC\u099F \u09AC\u09CD\u09AF\u09AC\u09B9\u09BE\u09B0 \u0995\u09B0\u09A4\u09C7 \u0986\u0997\u09C7 \u09A8\u09BF\u099A\u09C7\u09B0 \u099A\u09CD\u09AF\u09BE\u09A8\u09C7\u09B2(\u0997\u09C1\u09B2\u09CB)\u09A4\u09C7 \u099C\u09DF\u09C7\u09A8 \u0995\u09B0\u09C1\u09A8\u0964",
    "hi": "\U0001F512 \u092C\u0949\u091F \u0915\u093E \u0909\u092A\u092F\u094B\u0917 \u0915\u0930\u0928\u0947 \u0915\u0947 \u0932\u093F\u090F \u092A\u0939\u0932\u0947 \u0928\u0940\u091A\u0947 \u0926\u093F\u090F \u091A\u0948\u0928\u0932 \u0938\u0947 \u091C\u0941\u0921\u093C\u0947\u0902\u0964",
    "ar": "\U0001F512 \u0644\u0627\u0633\u062A\u062E\u062F\u0627\u0645 \u0627\u0644\u0628\u0648\u062A\u060C \u0627\u0646\u0636\u0645 \u0623\u0648\u0644\u0627\u064B \u0625\u0644\u0649 \u0627\u0644\u0642\u0646\u0648\u0627\u062A \u0623\u062F\u0646\u0627\u0647.",
}
JOIN_AFTER = {
    "en": "After joining, tap \u2705 Check join.",
    "bn": "\u099C\u09DF\u09C7\u09A8 \u0995\u09B0\u09BE\u09B0 \u09AA\u09B0\u09C7 \u2705 \u099C\u09DF\u09C7\u09A8 \u099A\u09C7\u0995 \u0995\u09B0\u09C1\u09A8 \u09AC\u09BE\u099F\u09A8\u09C7 \u0995\u09CD\u09B2\u09BF\u0995 \u0995\u09B0\u09C1\u09A8\u0964",
    "hi": "\u091C\u0941\u0921\u093C\u0928\u0947 \u0915\u0947 \u092C\u093E\u0926 \u2705 \u091C\u0949\u0907\u0928 \u091C\u093E\u0902\u091A\u0947\u0902 \u092A\u0930 \u091F\u0948\u092A \u0915\u0930\u0947\u0902\u0964",
    "ar": "\u0628\u0639\u062F \u0627\u0644\u0627\u0646\u0636\u0645\u0627\u0645\u060C \u0627\u0636\u063A\u0637 \u2705 \u062A\u062D\u0642\u0642 \u0645\u0646 \u0627\u0644\u0627\u0646\u0636\u0645\u0627\u0645.",
}


async def send_join_prompt(message, channels, context):
    """Join gate: blue inline Join-now buttons plus a Check-join reply keyboard."""
    uid = getattr(message, "chat_id", None) or context.user_data.get("uid") or 0
    lang = get_user_lang(uid) or DEFAULT_LANG
    token = CURRENT_LANG.set(lang)
    try:
        lines = [JOIN_INTRO.get(lang, JOIN_INTRO["en"]), ""]
        inline_rows = []
        for channel in channels:
            title = channel.get("title") or channel.get("username") or channel.get("chat_id") or "Channel"
            link = channel_link(channel)
            lines.append(f"\u2022 {title}")
            if link:
                inline_rows.append([{
                    "text": f"\u27A1\uFE0F {title}",
                    "url": link,
                    "style": "primary",
                }])
        lines.append("")
        lines.append(JOIN_AFTER.get(lang, JOIN_AFTER["en"]))
        text = "\n".join(lines)
        if inline_rows:
            await message.reply_text(text, reply_markup={"inline_keyboard": inline_rows})
        else:
            await message.reply_text(text)
        await message.reply_text(
            "\u2705 " + L("Check join"),
            reply_markup=green_reply_markup([[KeyboardButton("\u2705 " + L("Check join"))]]),
        )
    finally:
        CURRENT_LANG.reset(token)


def mark_onboarded(user_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))
        conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (int(user_id),))


async def send_language_prompt(chat_id, context):
    context.user_data["mode"] = "choose_language"
    await context.bot.send_message(
        chat_id=chat_id,
        text="\U0001F310 " + L("Choose your language / \u0986\u09AA\u09A8\u09BE\u09B0 \u09AD\u09BE\u09B7\u09BE \u09A8\u09BF\u09B0\u09CD\u09AC\u09BE\u099A\u09A8 \u0995\u09B0\u09C1\u09A8"),
        reply_markup=language_keyboard(),
    )


CLAIM_TEXT = {
    "en": ("\U0001F381 A gift for you: 3 days of Premium, free!\n\n"
           "Tap Claim now to open the web page and activate it on your account.\n\n"
           "\u26A0\uFE0F One free premium per device. VPN or cloned apps are not eligible."),
    "bn": ("\U0001F381 \u0986\u09AA\u09A8\u09BE\u09B0 \u099C\u09A8\u09CD\u09AF 3 \u09A6\u09BF\u09A8\u09C7\u09B0 \u09AB\u09CD\u09B0\u09BF \u09AA\u09CD\u09B0\u09BF\u09AE\u09BF\u09DF\u09BE\u09AE!\n\n"
           "Claim now \u09AC\u09BE\u099F\u09A8\u09C7 \u0995\u09CD\u09B2\u09BF\u0995 \u0995\u09B0\u09C7 \u0993\u09DF\u09C7\u09AC \u09AA\u09C7\u099C \u09A5\u09C7\u0995\u09C7 \u0986\u0995\u09CD\u099F\u09BF\u09AD\u09C7\u099F \u0995\u09B0\u09C1\u09A8\u0964\n\n"
           "\u26A0\uFE0F \u098F\u0995\u099F\u09BF \u09A1\u09BF\u09AD\u09BE\u0987\u09B8\u09C7 \u09B6\u09C1\u09A7\u09C1 \u098F\u0995\u09AC\u09BE\u09B0\u0987 \u09AB\u09CD\u09B0\u09BF \u09AA\u09CD\u09B0\u09BF\u09AE\u09BF\u09DF\u09BE\u09AE \u09A8\u09C7\u09DF\u09BE \u09AF\u09BE\u09AC\u09C7\u0964 VPN \u09AC\u09BE \u0985\u09CD\u09AF\u09BE\u09AA \u0995\u09CD\u09B2\u09CB\u09A8\u09C7 \u09A8\u09C7\u09DF\u09BE \u09AF\u09BE\u09AC\u09C7 \u09A8\u09BE\u0964"),
    "hi": ("\U0001F381 \u0906\u092A\u0915\u0947 \u0932\u093F\u090F \u0924\u094B\u0939\u092B\u093E: 3 \u0926\u093F\u0928 \u0915\u093E \u092E\u0941\u092B\u094D\u0924 \u092A\u094D\u0930\u0940\u092E\u093F\u092F\u092E!\n\n"
           "Claim now \u092A\u0930 \u091F\u0948\u092A \u0915\u0930\u0915\u0947 \u0935\u0947\u092C \u092A\u0947\u091C \u0938\u0947 \u0938\u0915\u094D\u0930\u093F\u092F \u0915\u0930\u0947\u0902\u0964\n\n"
           "\u26A0\uFE0F \u090F\u0915 \u0921\u093F\u0935\u093E\u0907\u0938 \u092A\u0930 \u0938\u093F\u0930\u094D\u092B \u090F\u0915 \u092C\u093E\u0930\u0964 VPN \u092F\u093E \u0915\u094D\u0932\u094B\u0928 \u0910\u092A \u092E\u093E\u0928\u094D\u092F \u0928\u0939\u0940\u0902\u0964"),
    "ar": ("\U0001F381 \u0647\u062F\u064A\u0629 \u0644\u0643: 3 \u0623\u064A\u0627\u0645 \u0628\u0631\u064A\u0645\u064A\u0648\u0645 \u0645\u062C\u0627\u0646\u064B\u0627!\n\n"
           "\u0627\u0636\u063A\u0637 Claim now \u0644\u0641\u062A\u062D \u0635\u0641\u062D\u0629 \u0627\u0644\u0648\u064A\u0628 \u0648\u062A\u0641\u0639\u064A\u0644\u0647.\n\n"
           "\u26A0\uFE0F \u0645\u0631\u0629 \u0648\u0627\u062D\u062F\u0629 \u0641\u0642\u0637 \u0644\u0643\u0644 \u062C\u0647\u0627\u0632. VPN \u0623\u0648 \u0627\u0644\u062A\u0637\u0628\u064A\u0642\u0627\u062A \u0627\u0644\u0645\u0646\u0633\u0648\u062E\u0629 \u063A\u064A\u0631 \u0645\u062F\u0639\u0648\u0645\u0629."),
}


def web_base_url():
    url = (get_setting("web_base_url") or os.getenv("WEB_BASE_URL", "")).strip().rstrip("/")
    if url:
        return url
    return f"http://localhost:{os.getenv('WEB_PORT', '8080')}"


def claim_premium_url(user_id):
    return f"{web_base_url()}/claim_premium/{int(user_id)}"


async def send_claim_prompt(chat_id, context, user_id):
    context.user_data["mode"] = "claim_offer"
    lang = CURRENT_LANG.get()
    url = claim_premium_url(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=CLAIM_TEXT.get(lang, CLAIM_TEXT["en"]),
        reply_markup={"inline_keyboard": [[{
            "text": "\U0001F381 " + L("Claim now"),
            "url": url,
            "style": "success",
        }]]},
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=L("Claim 3 days premium") + " / " + L("Skip"),
        reply_markup=green_reply_markup([[
            KeyboardButton("\U0001F381 " + L("Claim 3 days premium")),
            KeyboardButton(L("Skip")),
        ]]),
    )


async def continue_onboarding(update, context):
    """Language -> first-time premium offer -> main menu."""
    uid = update.effective_user.id
    lang = get_user_lang(uid)
    if lang:
        CURRENT_LANG.set(lang)
    if not lang:
        await send_language_prompt(uid, context)
        return
    with db() as conn:
        row = conn.execute(
            "SELECT onboarded, premium_claimed FROM users WHERE user_id=?", (uid,)
        ).fetchone()
    onboarded = int(row["onboarded"] or 0) if row else 0
    claimed = int(row["premium_claimed"] or 0) if row else 0
    if not onboarded:
        if not claimed:
            await send_claim_prompt(uid, context, uid)
            return
        mark_onboarded(uid)
    context.user_data["mode"] = None
    await update.effective_message.reply_text(
        "\U0001F44B " + L("Welcome to Premium Emoji Bot\nPlease choose an option from the menu below."),
        reply_markup=main_menu_for(update, context),
    )


async def ensure_joined(update, context):
    """Gate every bot action behind channel membership."""
    user = getattr(update, "effective_user", None)
    if not user:
        return True
    if is_admin(user.id):
        return True
    if not get_channels():
        return True
    missing = await missing_channels(context.bot, user.id)
    if not missing:
        return True
    message = getattr(update, "effective_message", None)
    if message is not None:
        await send_join_prompt(message, missing, context)
    return False


def category_names():
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT name FROM categories ORDER BY display_order, category_id").fetchall()]


def get_category_emojis(category):
    with db() as conn:
        rows = conn.execute("SELECT e.emoji_id, e.alt FROM emojis e JOIN emoji_categories c ON c.emoji_id=e.emoji_id WHERE c.category=? ORDER BY c.sort_order, e.sort_order, e.emoji_id", (category,)).fetchall()
    return [dict(r) for r in rows]


def serial_to_ids(text, rows):
    ids = []
    # Explicit ID values
    ids.extend(parse_ids(text))
    # serial id1, emoji serial id2, serial 1-5, or comma-separated serials
    for a, b in re.findall(r"(?:emoji\s*)?serial\s*(?:id)?\s*(\d+)\s*(?:[-–]\s*(\d+))?", text, re.I):
        lo, hi = int(a), int(b or a)
        if lo > hi:
            lo, hi = hi, lo
        ids.extend(str(r['emoji_id']) for n, r in enumerate(rows, 1) if lo <= n <= hi)
    for m in re.finditer(r"(?i)(?:emoji\s*)?serials?\s*[:=]?\s*(\d+(?:\s*,\s*\d+)+)", text):
        for n in map(int, re.findall(r"\d+", m.group(1))):
            if 1 <= n <= len(rows): ids.append(str(rows[n-1]['emoji_id']))
    return list(dict.fromkeys(ids))


def parse_ids(text):
    return list(dict.fromkeys(re.findall(r"(?<!\d)\d{5,30}(?!\d)", text or "")))


def get_pack_name(text: str):
    match = re.search(
        r"(?:https?://)?(?:www\.)?t\.me/addemoji/([A-Za-z0-9_]+)",
        (text or "").strip(), re.IGNORECASE,
    )
    if match:
        return match.group(1)
    if (text or "").strip().startswith("tg://addemoji?"):
        params = parse_qs(urlparse(text.strip()).query)
        return params.get("set", [None])[0]
    return None


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def get_custom_emoji_alt(document) -> str:
    for attr in getattr(document, "attributes", []):
        alt = getattr(attr, "alt", None)
        if alt:
            return alt
    return "😀"


def extract_entities(message):
    found = []
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == MessageEntity.CUSTOM_EMOJI and getattr(entity, "custom_emoji_id", None):
            alt = text[entity.offset:entity.offset + entity.length]
            found.append((str(entity.custom_emoji_id), alt or "😀"))
    return found


def paged_nav(prefix, page, total_pages, extra=""):
    """Build [Previous] [page x/y] [Next] with wrap-around pagination.

    The middle button is a dummy that shows the current/total page and does
    nothing when tapped. When there is only one page, only the page button is
    shown (no Previous/Next).
    """
    nav = []
    if total_pages > 1:
        nav.append(InlineKeyboardButton("◀️ Previous", callback_data=f"{prefix}:{extra}{(page - 1) % total_pages}"))
    nav.append(InlineKeyboardButton(f"page {page + 1}/{total_pages}", callback_data="dummy_page"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"{prefix}:{extra}{(page + 1) % total_pages}"))
    return nav


def emoji_page_keyboard(rows, page, favorites=False, active_category=None, total_count=None):
    """Inline pagination: Previous / page x/y / Next (wrap-around)."""
    total = len(rows) if total_count is None else total_count
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    page_prefix = "fpage" if favorites else "page"
    nav = paged_nav(page_prefix, page, total_pages)
    return green_inline_markup([nav])


def emoji_category_keyboard(favorites=False):
    """Reply keyboard for the Make a post category menu (Back on the last row)."""
    layout = [[("all_emoji", "All emoji"), ("favorite", "Favorite")]]
    names = category_names()
    for i in range(0, len(names), 2):
        layout.append([(f"cat:{name}", name) for name in names[i:i + 2]])
    layout.append([("back", "Back")])
    return scoped_keyboard("category", layout)


def favorite_keyboard():
    """Reply keyboard for the Favorite menu: Add / Remove on row 1, Back on row 2."""
    return scoped_keyboard("favorites", [
        [("add_favorite", "Add favorite"), ("remove_favorite", "Remove favorite")],
        [("back", "Back")],
    ])


def bot_settings_keyboard():
    """Admin tools moved out of the main menu."""
    return scoped_keyboard("bot_settings", [
        [("remove_emoji", "Remove emoji"), ("category", "\U0001F4C1 Category")],
        [("back", "Back")],
    ])


def admin_categories_keyboard():
    layout = [[("create_category", "Create category")]]
    names = category_names()
    for i in range(0, len(names), 2):
        layout.append([(f"cat:{name}", name) for name in names[i:i + 2]])
    layout.append([("back_to_menu", "Back to menu")])
    return scoped_keyboard("admin_categories", layout)


def admin_category_page_keyboard():
    return scoped_keyboard("admin_category_page", [
        [("add_to_category", "Add to category"), ("remove_from_category", "Remove from category")],
        [("delete_category", "Delete category")],
        [("back_to_categories", "Back to categories")],
    ])


def task_menu_markup():
    return scoped_keyboard("task", [
        [("claim_daily", "Claim daily reward")],
        [("back", "Back")],
    ])


def refer_menu_markup():
    return scoped_keyboard("refer", [
        [("refer_history", "Refer history"), ("leader_board", "Leader board")],
        [("back_to_main", "Back to main")],
    ])


EMOJI_INSTRUCTION = "Please type your message and add emoji id or srl id then send the message"


async def ensure_emoji_keyboard(message, context, favorites=False, force=False):
    """Send the category/favorite reply keyboard only when its mode changes."""
    wanted = "favorites" if favorites else "normal"
    if (not force
            and context.user_data.get("emoji_keyboard_mode") == wanted
            and context.user_data.get("emoji_keyboard_message_id")):
        return
    old_id = context.user_data.get("emoji_keyboard_message_id")
    if old_id:
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=old_id)
        except TelegramError:
            pass
    sent = await message.reply_text(
        EMOJI_INSTRUCTION,
        reply_markup=favorite_keyboard() if favorites else emoji_category_keyboard(),
    )
    context.user_data["emoji_keyboard_message_id"] = sent.message_id
    context.user_data["emoji_keyboard_mode"] = wanted


async def delete_emoji_view(chat_id, context):
    """Delete the emoji list message and the reply-keyboard message."""
    ids = list(context.user_data.get("emoji_list_message_ids", []))
    keyboard_id = context.user_data.get("emoji_keyboard_message_id")
    if keyboard_id:
        ids.append(keyboard_id)
    for message_id in set(ids):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass
    context.user_data["emoji_list_message_ids"] = []
    context.user_data["emoji_keyboard_message_id"] = None
    context.user_data["emoji_keyboard_mode"] = None


async def show_emoji_page(target, context, page=0, favorites=False):
    active_category = context.user_data.get("active_category") if not favorites else None
    uid = context.user_data.get("uid")
    locked_count = 0
    if favorites:
        rows = get_emojis(favorites=True, user_id=uid)
        # Free users see only the first 5 favorites; the rest stay behind a lock.
        if uid and not is_premium(uid) and len(rows) > FAVORITE_LIMIT:
            locked_count = len(rows) - FAVORITE_LIMIT
            rows = rows[:FAVORITE_LIMIT]
    elif active_category:
        rows = get_category_emojis(active_category)
    else:
        rows = get_emojis()
    total = len(rows)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    subset = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    title = "⭐ Favorite Emoji" if favorites else (f"📂 {active_category}" if active_category else "📝 Make a post — Emoji list")
    entities = []
    text = f"{title}\n📄 Page {page + 1} · Total: {total}\n\n"
    if not subset:
        text += "No emojis found. Use ➕ Add emoji to add some first."
    else:
        global_serials = {str(r["emoji_id"]): i for i, r in enumerate(get_emojis(), start=1)}
        for item in subset:
            index = global_serials.get(str(item["emoji_id"]), 0)
            alt = item.get("alt") or "😀"
            emoji_id = str(item["emoji_id"])
            prefix = f"{index}. "
            separator = " — "
            shown_id = "{" + emoji_id + "}"
            emoji_offset = utf16_len(text + prefix)
            id_offset = utf16_len(text + prefix + alt + separator)
            text += prefix + alt + separator + shown_id
            if item is not subset[-1]:
                text += "\n"
            entities.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=emoji_offset,
                length=utf16_len(alt), custom_emoji_id=emoji_id))
            # Telegram clients offer tap-to-copy for code-formatted text.
            entities.append(MessageEntity(type=MessageEntity.CODE, offset=id_offset,
                length=utf16_len(shown_id)))

    markup = emoji_page_keyboard(subset, page, favorites=favorites, active_category=active_category, total_count=total)
    if locked_count:
        markup.setdefault("inline_keyboard", []).append([{
            "text": f"{LOCK} Load more emoji ({locked_count} locked)",
            "callback_data": "favmore",
            "style": "primary",
        }])
    context.user_data["emoji_page"] = page
    context.user_data["showing_favorites"] = favorites
    if hasattr(target, "effective_message") and target.effective_message is not None:
        sent = await target.effective_message.reply_text(text, entities=entities or None, reply_markup=markup)
        context.user_data["emoji_list_message_ids"] = [sent.message_id]
    else:
        try:
            await target.edit_message_text(text, entities=entities or None, reply_markup=markup)
        except TelegramError as exc:
            # Telegram raises this when an edit would leave the message unchanged.
            if "message is not modified" not in str(exc).lower():
                raise
        context.user_data["emoji_list_message_ids"] = [target.message.message_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["uid"] = update.effective_user.id
    track_user(update)
    _lang = get_user_lang(update.effective_user.id)
    if _lang:
        CURRENT_LANG.set(_lang)
    if not await ensure_joined(update, context):
        return
    # Referral deep link: /start ref12345
    args = context.args or []
    if args and args[0].lower().startswith("ref"):
        try:
            referrer = int(args[0][3:])
        except ValueError:
            referrer = 0
        me = update.effective_user.id
        if referrer and referrer != me:
            with db() as conn:
                exists = conn.execute("SELECT 1 FROM referrals WHERE user_id=?", (me,)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO referrals(user_id, referrer_id) VALUES(?, ?)", (me, referrer))
                    conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer, me))
                    conn.execute("UPDATE users SET balance = balance + 5 WHERE user_id=?", (referrer,))
    await continue_onboarding(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    if not await ensure_joined(update, context):
        return
    await update.effective_message.reply_text(HELP_TEXT, reply_markup=main_menu_for(update, context))



async def show_category_page(target, context, category, page=0, admin=False):
    rows = get_category_emojis(category); total = len(rows); page = max(0, page)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    subset = rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    text = f"📂 Category: {category}\n📄 Page {page+1} · Total: {total}\n\n"
    entities = []
    global_serials = {str(r["emoji_id"]): i for i, r in enumerate(get_emojis(), start=1)}
    for item in subset:
        i = global_serials.get(str(item["emoji_id"]), 0)
        alt = item.get("alt") or "😀"
        prefix = f"{i}. "
        separator = " — "
        shown_id = "{" + str(item['emoji_id']) + "}"
        emoji_offset = utf16_len(text + prefix)
        id_offset = utf16_len(text + prefix + alt + separator)
        text += prefix + alt + separator + shown_id
        if item is not subset[-1]:
            text += "\n"
        entities.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=emoji_offset, length=utf16_len(alt), custom_emoji_id=str(item['emoji_id'])))
        entities.append(MessageEntity(type=MessageEntity.CODE, offset=id_offset, length=utf16_len(shown_id)))
    buttons = []
    idx = category_names().index(category)
    buttons.append(paged_nav('cpage' if admin else 'ucpage', page, total_pages, extra=f"{idx}:"))
    markup=green_inline_markup(buttons) if buttons else None
    context.user_data['current_category']=category
    if hasattr(target, 'edit_message_text'):
        await target.edit_message_text(text or f"📂 {category}\nNo emojis found.", entities=entities or None, reply_markup=markup)
        if admin:
            await target.message.reply_text(
                "🛠️ Category actions are on the keyboard below.",
                reply_markup=admin_category_page_keyboard(),
            )
    else:
        await target.effective_message.reply_text(text or f"📂 {category}\nNo emojis found.", entities=entities or None, reply_markup=markup)
        if admin:
            await target.effective_message.reply_text(
                "🛠️ Category actions are on the keyboard below.",
                reply_markup=admin_category_page_keyboard(),
            )



async def show_category_missing_page(message, context, page=0):
    category = context.user_data.get("current_category")
    if not category:
        await message.reply_text("No category selected.")
        return
    present = {r["emoji_id"] for r in get_category_emojis(category)}
    missing = [r for r in get_emojis() if r["emoji_id"] not in present]
    total = len(missing)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    subset = missing[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    header = f"📥 Category: {category} — Available emojis to add\n📄 Page {page + 1} · Total: {total}\n\n"
    text = header
    entities = []
    for serial, row in enumerate(subset, start=page * PAGE_SIZE + 1):
        alt = row.get("alt") or "😀"
        prefix = f"{serial}. "
        separator = " — "
        shown_id = "{" + str(row["emoji_id"]) + "}"
        offset = utf16_len(text + prefix)
        id_offset = utf16_len(text + prefix + alt + separator)
        text += f"{prefix}{alt}{separator}{shown_id}\n"
        entities.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=offset, length=utf16_len(alt), custom_emoji_id=str(row["emoji_id"])))
        entities.append(MessageEntity(type=MessageEntity.CODE, offset=id_offset, length=utf16_len(shown_id)))
    if not subset:
        text += "There are no new emojis to add to this category.\n"
    text += "\nTo add emojis, send an ID, `emoji serial id1`, or a serial range such as `1-5`."
    buttons = []
    buttons.append(paged_nav("caddpage", page, total_pages))
    markup = green_inline_markup(buttons) if buttons else None
    await message.reply_text(text, entities=entities, reply_markup=markup)
    await message.reply_text(
        "🛠️ Category actions are on the keyboard below.",
        reply_markup=admin_category_page_keyboard(),
    )


async def delete_post_step_messages(chat_id, context, include_emoji_list=False, extra_message_ids=None):
    """Delete tracked post-builder prompts/previews and optional callback message IDs."""
    ids = list(context.user_data.get("post_preview_message_ids", []))
    if include_emoji_list:
        ids += list(context.user_data.get("emoji_list_message_ids", []))
    if extra_message_ids:
        ids += list(extra_message_ids)
    seen = set()
    for message_id in ids:
        if message_id in seen:
            continue
        seen.add(message_id)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass
    context.user_data["post_preview_message_ids"] = []
    if include_emoji_list:
        context.user_data["emoji_list_message_ids"] = []


def post_step_keyboard(rows, color_map=None):
    """Reply keyboard for a post-builder step with optional per-button colours."""
    styled = []
    for row in rows:
        styled_row = []
        for label in row:
            item = {"text": tr_label(label)}
            style = color_style((color_map or {}).get(label, "green"))
            if style:
                item["style"] = style
            styled_row.append(item)
        styled.append(styled_row)
    return {"keyboard": styled, "resize_keyboard": True}


def post_color_options(premium=True):
    """Colour choices for a post button. Free users only get Normal."""
    if premium:
        return (
            ["Normal", "Red", "Blue", "Green"],
            {"Normal": "normal", "Red": "red", "Blue": "blue", "Green": "green"},
        )
    return (
        ["Normal", f"{LOCK} Red", f"{LOCK} Blue", f"{LOCK} Green"],
        {"Normal": "normal", f"{LOCK} Red": "normal",
         f"{LOCK} Blue": "normal", f"{LOCK} Green": "normal"},
    )


async def show_generating(chat_id, context, base="generating post"):
    """Show 'generating post .' then animate .. ... with a loading effect."""
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=f"{base} .")
    except TelegramError:
        return
    for dots in ("..", "...", ".", "..", "..."):
        await asyncio.sleep(0.35)
        try:
            await msg.edit_text(f"{base} {dots}")
        except TelegramError:
            pass
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except TelegramError:
        pass


async def send_post_preview_raw(chat_id, context):
    """Send the current draft as a post (text + all images + inline buttons)."""
    draft = context.user_data.get("post_draft")
    if not draft:
        return []
    text = draft.get("text", "")
    entities = draft_entities(draft)
    photos = draft.get("photos", [])
    keyboard = post_button_keyboard(draft)
    ids = []
    if not photos:
        message = await context.bot.send_message(
            chat_id=chat_id, text=text, entities=entities or None, reply_markup=keyboard,
        )
        ids.append(message.message_id)
    elif len(photos) == 1:
        if len(text) <= 1024:
            message = await context.bot.send_photo(
                chat_id=chat_id, photo=photos[0], caption=text,
                caption_entities=entities or None, reply_markup=keyboard,
            )
            ids.append(message.message_id)
        else:
            message = await context.bot.send_photo(chat_id=chat_id, photo=photos[0])
            ids.append(message.message_id)
            message = await context.bot.send_message(
                chat_id=chat_id, text=text, entities=entities or None, reply_markup=keyboard,
            )
            ids.append(message.message_id)
    else:
        media = [InputMediaPhoto(photo) for photo in photos[:10]]
        sent = await context.bot.send_media_group(chat_id=chat_id, media=media)
        ids.extend(message.message_id for message in sent)
        message = await context.bot.send_message(
            chat_id=chat_id, text=text, entities=entities or None, reply_markup=keyboard,
        )
        ids.append(message.message_id)
    return ids


async def send_post_step(chat_id, context, prompt_text, rows, mode=None, color_map=None):
    """Render the draft, then a prompt with the step's reply keyboard."""
    await delete_post_step_messages(chat_id, context)
    ids = await send_post_preview_raw(chat_id, context)
    prompt = await context.bot.send_message(
        chat_id=chat_id, text=prompt_text,
        reply_markup=post_step_keyboard(rows, color_map),
    )
    ids.append(prompt.message_id)
    context.user_data["post_preview_message_ids"] = ids
    if mode is not None:
        context.user_data["mode"] = mode


def remove_watermark(draft):
    draft["text"] = draft.get("base_text", draft.get("text", ""))
    draft["entities"] = list(draft.get("base_entities", []))
    draft["watermark"] = False


async def post_start_image_step(chat_id, context):
    await send_post_step(
        chat_id, context,
        "📷 Send a photo now, or tap Skip.",
        [["Skip"]], mode="post_wait_image",
    )


async def post_image_review_step(chat_id, context):
    draft = context.user_data.get("post_draft") or {}
    photos = draft.get("photos", [])
    premium = is_premium(context.user_data.get("uid") or 0)
    limit = MAX_POST_IMAGES if premium else FREE_POST_IMAGES
    row = ["Replace image"]
    if premium:
        if len(photos) < MAX_POST_IMAGES:
            row.append("Add more image")
    else:
        row.append(f"{LOCK} Add more image")
    row.append("Next")
    prompt = f"\U0001F5BC\uFE0F {len(photos)}/{limit} image(s) added. Choose an option."
    if not premium:
        prompt += f"\n{LOCK} Free accounts can add {FREE_POST_IMAGES} image per post."
    await send_post_step(
        chat_id, context, prompt, [row], mode="post_image_review",
    )


async def post_go_to_button_name(chat_id, context):
    draft = context.user_data.get("post_draft")
    if not draft:
        await context.bot.send_message(chat_id=chat_id, text="Please create your post text first.")
        return
    context.user_data["post_adding_more"] = False
    await send_post_step(
        chat_id, context,
        "🔘 Enter a button name, or tap Skip to continue without adding a button.",
        [["Skip"]], mode="post_button_name",
    )


async def post_add_button_step(chat_id, context):
    draft = context.user_data.get("post_draft")
    if not draft:
        await context.bot.send_message(chat_id=chat_id, text="Draft not found.")
        return
    if len(draft.get("buttons", [])) >= MAX_POST_BUTTONS:
        await context.bot.send_message(chat_id=chat_id, text=f"You can add up to {MAX_POST_BUTTONS} buttons.")
        return
    context.user_data["post_adding_more"] = True
    await send_post_step(
        chat_id, context,
        "🔘 Enter the new button name.",
        [["Skip"]], mode="post_button_name",
    )


async def post_go_to_buttons(chat_id, context):
    draft = context.user_data.get("post_draft")
    if not draft:
        await context.bot.send_message(chat_id=chat_id, text="Post draft not found.")
        return
    buttons = draft.get("buttons", [])
    if not buttons:
        await post_go_to_watermark(chat_id, context)
        return
    premium = is_premium(context.user_data.get("uid") or 0)
    add_label = "Add more button" if premium else f"{LOCK} Add more button"
    if len(buttons) == 1:
        rows = [[add_label, "Edit button", "Next"]]
    else:
        rows = [[add_label, "Edit button"], ["Button setting", "Next"]]
    prompt = f"\U0001F517 {len(buttons)} button(s) added. Choose an option."
    if not premium:
        prompt += f"\n{LOCK} Free accounts can add {FREE_POST_BUTTONS} button per post."
    await send_post_step(
        chat_id, context, prompt, rows, mode="post_buttons",
    )


async def post_edit_select_step(chat_id, context):
    draft = context.user_data.get("post_draft") or {}
    buttons = draft.get("buttons", [])
    rows = [[button.get("name", "Button")] for button in buttons]
    rows.append(["Cancel"])
    await send_post_step(
        chat_id, context,
        "✏️ Choose a button to edit.",
        rows, mode="post_edit_select",
    )


async def post_go_to_watermark(chat_id, context):
    await send_post_step(
        chat_id, context,
        "💧 Choose a watermark option for the final post.",
        [["Continue with watermark", "Continue without watermark"]],
        mode="post_watermark_choice",
    )


async def post_go_to_ready(chat_id, context):
    await send_post_step(
        chat_id, context,
        "✅ Your post is ready. Choose where to send it.",
        [["Post channel", "Forward"], ["Back to main"]],
        mode="post_ready",
    )


async def post_go_to_channels(chat_id, context):
    uid = context.user_data.get("uid") or 0
    channels = get_user_channels(uid)
    premium = is_premium(uid)
    rows = []
    if channels:
        labels = [
            channel.get("title") or channel.get("username") or channel.get("chat_id") or "Channel"
            for channel in channels
        ]
        if not premium:
            labels = [f"{LOCK} {label}" for label in labels]
        for i in range(0, len(labels), 2):
            rows.append(labels[i:i + 2])
        rows.append(["Add channel" if premium else f"{LOCK} Add channel", "Remove channel"])
        rows.append(["Cancel"])
        prompt = "\U0001F4E2 Choose a channel to publish this post."
        if not premium:
            prompt += f"\n{LOCK} Channel publishing is a premium feature. Buy premium to unlock it."
    else:
        rows = [["Add channel" if premium else f"{LOCK} Add channel"], ["Cancel"]]
        prompt = "\U0001F4E2 You have not added any channel yet. Tap Add channel to add one."
        if not premium:
            prompt += f"\n{LOCK} Adding channels is a premium feature."
    await send_post_step(chat_id, context, prompt, rows, mode="post_channel_list")


async def post_replace_image_step(chat_id, context):
    if not context.user_data.get("post_draft"):
        await context.bot.send_message(chat_id=chat_id, text="Post draft not found. Please start again from Make a post.")
        return
    await send_post_step(
        chat_id, context,
        "📷 Send the new photo. It will replace the current photo(s).",
        [["Cancel"]], mode="post_replace_image",
    )


async def send_add_bot_prompt(chat_id, context):
    bot_username = context.bot.username or ""
    if bot_username:
        url = (f"https://t.me/{bot_username}?startchannel=true"
               "&admin=post_messages+edit_messages+delete_messages")
    else:
        url = "https://t.me"
    markup = {"inline_keyboard": [[{"text": "➕ Add me to your channel", "url": url}]]}
    await context.bot.send_message(
        chat_id=chat_id,
        text="Tap the button below to add the bot as an administrator of your channel.",
        reply_markup=markup,
    )


async def check_and_add_channel(chat_id, context, user_id, parsed):
    """Verify bot + user admin rights, then store the channel (max 5)."""
    target = channel_post_target(parsed)
    if not target:
        await send_post_step(chat_id, context, "❌ Invalid channel details.", [["Cancel"]], mode="post_channel_check")
        return
    if not await bot_is_admin(context.bot, target):
        await send_post_step(
            chat_id, context,
            "⚠️ The bot is not an administrator in this channel. Add the bot as an admin, then tap Check again.",
            [["Add me to your channel"], ["Check again"], ["Cancel"]],
            mode="post_channel_check",
        )
        return
    if not await user_is_admin(context.bot, target, user_id):
        await send_post_step(
            chat_id, context,
            "⚠️ You are not an administrator in this channel. Only channel admins can add it.",
            [["Check again"], ["Cancel"]],
            mode="post_channel_check",
        )
        return
    title = parsed.get("title") or parsed.get("username") or parsed.get("chat_id")
    try:
        chat = await context.bot.get_chat(target)
        title = getattr(chat, "title", None) or title
    except TelegramError:
        pass
    ok, count = add_user_channel(
        user_id, parsed.get("chat_id", ""), parsed.get("username", ""),
        title, parsed.get("link", ""),
    )
    if not ok:
        await send_post_step(
            chat_id, context,
            f"⚠️ You can add up to {MAX_USER_CHANNELS} channels.",
            [["Cancel"]], mode="post_channel_check",
        )
        return
    context.user_data.pop("pending_channel", None)
    await context.bot.send_message(chat_id=chat_id, text=f"✅ Channel added: {title}")
    await post_go_to_channels(chat_id, context)


async def send_draft_to_target(bot, target, draft):
    """Publish a draft (text + images + inline buttons) to any chat/channel."""
    text = draft.get("text", "")
    entities = draft_entities(draft)
    photos = draft.get("photos", [])
    keyboard = post_button_keyboard(draft)
    if not photos:
        await bot.send_message(chat_id=target, text=text, entities=entities or None, reply_markup=keyboard)
    elif len(photos) == 1:
        if len(text) <= 1024:
            await bot.send_photo(
                chat_id=target, photo=photos[0], caption=text,
                caption_entities=entities or None, reply_markup=keyboard,
            )
        else:
            await bot.send_photo(chat_id=target, photo=photos[0])
            await bot.send_message(chat_id=target, text=text, entities=entities or None, reply_markup=keyboard)
    else:
        media = [InputMediaPhoto(photo) for photo in photos[:10]]
        await bot.send_media_group(chat_id=target, media=media)
        await bot.send_message(chat_id=target, text=text, entities=entities or None, reply_markup=keyboard)


async def post_publish_to_channel(chat_id, context, channel):
    draft = context.user_data.get("post_draft")
    if not draft:
        await context.bot.send_message(chat_id=chat_id, text="Post draft not found.")
        return
    target = channel_post_target(channel)
    try:
        await send_draft_to_target(context.bot, target, draft)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Post published to {channel.get('title') or target}.",
        )
    except Exception as exc:
        print("Channel publish error:", repr(exc))
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Could not publish to {channel.get('title') or target}. "
                 "Make sure the bot is an admin with post permission.",
        )


async def post_publish(chat_id, context):
    """Generate the final post and move to the ready menu."""
    draft = context.user_data.get("post_draft")
    if not draft:
        await context.bot.send_message(chat_id=chat_id, text="Post draft not found.")
        return
    await post_go_to_ready(chat_id, context)


async def show_welcome(chat_id, context):
    """Delete the emoji list/keyboard and return to the main menu."""
    await delete_emoji_view(chat_id, context)
    context.user_data.pop("active_category", None)
    context.user_data.pop("showing_favorites", None)
    context.user_data["mode"] = None
    await context.bot.send_message(
        chat_id=chat_id,
        text="👋 " + L("Welcome to Premium Emoji Bot\nPlease choose an option from the menu below."),
        reply_markup=main_menu(context.user_data.get("uid")),
    )


REF_PAGE_SIZE = 20


def refer_history_rows(uid):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.created_at, u.user_id, u.username, u.first_name, u.balance "
            "FROM referrals r LEFT JOIN users u ON u.user_id = r.user_id "
            "WHERE r.referrer_id = ? ORDER BY r.created_at DESC",
            (int(uid),),
        ).fetchall()]


async def show_refer_history(chat_id, context, uid, page=0):
    rows = refer_history_rows(uid)
    total = len(rows)
    total_pages = max(1, math.ceil(total / REF_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    subset = rows[page * REF_PAGE_SIZE:(page + 1) * REF_PAGE_SIZE]
    lines = [f"👥 Refer history — page {page + 1}/{total_pages}", ""]
    if not subset:
        lines.append("No referrals yet. Share your link to invite friends!")
    else:
        for index, row in enumerate(subset, start=page * REF_PAGE_SIZE + 1):
            name = row.get("first_name") or (
                "@" + row["username"] if row.get("username") else str(row["user_id"])
            )
            lines.append(f"{index}. {name} (ID: {row['user_id']})")
            lines.append(
                f"   Joined: {row['created_at']} · Bonus: +5 · Balance: {int(row['balance'] or 0)}"
            )
    markup = green_inline_markup([paged_nav("refhist", page, total_pages)])
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=markup)


async def show_refer_leaderboard(chat_id, context):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT r.referrer_id, COUNT(*) AS c, u.username, u.first_name "
            "FROM referrals r LEFT JOIN users u ON u.user_id = r.referrer_id "
            "GROUP BY r.referrer_id ORDER BY c DESC, r.referrer_id LIMIT 10",
        ).fetchall()]
    lines = ["🏆 Refer leader board — Top 10", ""]
    if not rows:
        lines.append("No referrals yet.")
    else:
        for index, row in enumerate(rows, 1):
            name = row.get("first_name") or (
                "@" + row["username"] if row.get("username") else str(row["referrer_id"])
            )
            lines.append(f"{index}. {name} — {row['c']} referral(s) · {row['c'] * 5} bonus")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


def favorite_cancel_keyboard():
    return green_reply_markup([[KeyboardButton("Cancel")]])


ORDERS_PER_PAGE = 5


def user_orders_page(user_id, page=0, per_page=ORDERS_PER_PAGE):
    orders = get_user_orders(user_id)
    total_pages = max(1, math.ceil(len(orders) / per_page))
    page = max(0, min(int(page), total_pages - 1))
    subset = orders[page * per_page:(page + 1) * per_page]
    lines = [f"\U0001F9FE My orders ({len(orders)} total)", ""]
    if not subset:
        lines.append("You have not placed any order yet.")
    for order in subset:
        lines.append(order_detail_text(order))
        lines.append("\u2500" * 18)
    markup = green_inline_markup([paged_nav("myord", page, total_pages)]) if total_pages > 1 else None
    return "\n".join(lines).strip(), markup


async def show_my_orders(chat_id, context, user_id, page=0):
    text, markup = user_orders_page(user_id, page)
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=markup,
    )


def admin_orders_page(page=0, per_page=ORDERS_PER_PAGE):
    orders = get_orders("pending", limit=500)
    total_pages = max(1, math.ceil(len(orders) / per_page))
    page = max(0, min(int(page), total_pages - 1))
    subset = orders[page * per_page:(page + 1) * per_page]
    lines = [f"\U0001F4E6 Pending orders ({len(orders)})", ""]
    if not subset:
        lines.append("No pending orders right now. \u2705")
    for order in subset:
        lines.append(order_detail_text(order))
        lines.append("\u2500" * 18)
    rows = []
    for order in subset:
        rows.append([
            {"text": f"\u2705 Approve #{order['id']}", "callback_data": f"ord:approve:{order['id']}"},
            {"text": f"\u274c Reject #{order['id']}", "callback_data": f"ord:reject:{order['id']}"},
        ])
    if total_pages > 1:
        rows.append(paged_nav("admord", page, total_pages))
    markup = {"inline_keyboard": rows} if rows else None
    return "\n".join(lines).strip(), markup


async def show_admin_orders(chat_id, context, page=0):
    text, markup = admin_orders_page(page)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


async def notify_order_result(bot, order, approved):
    if not order:
        return
    if approved:
        msg = (
            "\u2705 \u0986\u09AA\u09A8\u09BE\u09B0 \u0993\u09B0\u09CD\u09A1\u09BE\u09B0 \u0995\u09A8\u09AB\u09BE\u09B0\u09CD\u09AE \u09B9\u09AF\u09BC\u09C7\u099B\u09C7!\n\n"
            f"\U0001F9FE Order #{order['id']}\n"
            f"Package: {order['package_label']}\n"
            f"Price: {order['price']} taka\n"
        )
        if order["kind"] == "credit":
            msg += f"\U0001F4B0 Credit added: +{order['amount']}\nBalance: {user_balance(order['user_id'])}"
        elif order["kind"] == "premium":
            msg += (
                f"\u2b50 Premium package: {order['amount']} days\n\n"
                "\U0001F4A1 Tap the button below to activate premium on your account."
            )
        else:
            msg += f"\u2764\ufe0f FF auto like: {order['amount']} likes will be delivered."
    else:
        msg = (
            "\u274c \u09A6\u09C1\u0983\u0996\u09BF\u09A4, \u0986\u09AA\u09A8\u09BE\u09B0 \u0985\u09B0\u09CD\u09A1\u09BE\u09B0\u099F\u09BF \u09AC\u09BE\u09A4\u09BF\u09B2 \u0995\u09B0\u09BE \u09B9\u09AF\u09BC\u09C7\u099B\u09C7\u0964\n\n"
            f"\U0001F9FE Order #{order['id']}\n"
            f"Package: {order['package_label']}\n\n"
            "\u09AF\u09A6\u09BF \u099F\u09BE\u0995\u09BE \u0995\u09BE\u099F\u09BE \u09B9\u09AF\u09BC\u09C7 \u09A5\u09BE\u0995\u09C7 \u09A4\u09AC\u09C7 Support \u098F \u09AF\u09CB\u0997\u09BE\u09AF\u09CB\u0997 \u0995\u09B0\u09C1\u09A8\u0964"
        )
    markup = None
    if approved and order["kind"] == "premium" and not int(order.get("activated") or 0):
        markup = {"inline_keyboard": [[{
            "text": "\U0001F48E Active premium now",
            "callback_data": f"prem:activate:{order['id']}",
            "style": "success",
        }]]}
    try:
        await bot.send_message(chat_id=int(order["user_id"]), text=msg, reply_markup=markup)
    except Exception as exc:
        print("notify_order_result error:", repr(exc))


PREMIUM_LOCK_TEXT = (
    "\U0001F512 This feature is available for Premium members only.\n\n"
    "\U0001F48E Premium unlocks:\n"
    "\u2022 Unlimited favorite emojis\n"
    "\u2022 Up to 5 images per post\n"
    "\u2022 Unlimited buttons per post\n"
    "\u2022 Red / Blue / Green button colours\n"
    "\u2022 Add & publish to your own channels\n\n"
    "Tap Buy now to see the premium packages."
)


def premium_prompt_markup():
    return {"inline_keyboard": [[{
        "text": "\U0001F48E Buy now",
        "callback_data": "prem:buy",
        "style": "success",
    }]]}


async def send_premium_prompt(chat_id, context, text=None):
    await context.bot.send_message(
        chat_id=chat_id,
        text=text or PREMIUM_LOCK_TEXT,
        reply_markup=premium_prompt_markup(),
    )


def shop_menu_markup():
    return scoped_keyboard("shop", [
        [("buy_credit", "Buy credit"), ("buy_premium", "Buy premium")],
        [("buy_ff", "Buy FF like"), ("gen_redeem", "Generate redeem code")],
        [("back_to_main", "Back to main")],
    ])


def payment_method_markup(selected=None):
    """Payment menu. The chosen method keeps a tick instead of hiding the menu."""
    selected = (selected or "").strip().lower()
    layout = [[("bikash", "Bikash"), ("nagad", "Nagad")],
              [("other", "Other")],
              [("back_to_package", "Back to package")]]
    markup = scoped_keyboard("payment", layout)
    if selected:
        for row in markup["keyboard"]:
            for item in row:
                if unL(item["text"]).strip().lower() == selected:
                    item["text"] = "\u2705 " + item["text"]
    return markup


def _code_entity(text, needle, start=0):
    """Build a tap-to-copy entity for the first occurrence of needle."""
    index = text.find(needle, start)
    if index < 0:
        return None
    return MessageEntity(
        type=MessageEntity.CODE,
        offset=utf16_len(text[:index]),
        length=utf16_len(needle),
    )


def build_payment_info(package, method):
    """Return (text, entities) with a copyable number and amount."""
    number = get_setting("bikash_number" if method == "Bikash" else "nagad_number")
    amount = f"{package.get('price', 0)}"
    app = "Bikash" if method == "Bikash" else "Nagad"
    dial = "*247#" if method == "Bikash" else "*167#"
    lines = [
        "\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557",
        "\U0001F48E PAYMENT INFORMATION \U0001F48E",
        "\u255A\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255D",
        "\U0001F44B Welcome Dear Customer!",
        "\u0986\u09AA\u09A8\u09BE\u09B0 \u0985\u09B0\u09CD\u09A1\u09BE\u09B0 \u0995\u09A8\u09AB\u09BE\u09B0\u09CD\u09AE \u0995\u09B0\u09A4\u09C7 \u09A8\u09BF\u099A\u09C7\u09B0 \u09A8\u09BE\u09AE\u09CD\u09AC\u09BE\u09B0\u09C7 \u09AA\u09C7\u09AE\u09C7\u09A8\u09CD\u099F \u09B8\u09AE\u09CD\u09AA\u09A8\u09CD\u09A8 \u0995\u09B0\u09C1\u09A8\u0964 \u09AA\u09C7\u09AE\u09C7\u09A8\u09CD\u099F\u09C7\u09B0 \u0986\u0997\u09C7 \u09A4\u09A5\u09CD\u09AF\u0997\u09C1\u09B2\u09CB \u09AD\u09BE\u09B2\u09CB\u09AD\u09BE\u09AC\u09C7 \u09A6\u09C7\u0996\u09C7 \u09A8\u09BF\u09A8\u0964",
        "",
        "\U0001F4CB Payment Instructions",
        f"1. {number} \u09A8\u09BE\u09AE\u09CD\u09AC\u09BE\u09B0\u099F\u09BF \u0995\u09AA\u09BF \u0995\u09B0\u09C1\u09A8 ",
        "",
        f"2. {app} app \u099F\u09BF \u0993\u09AA\u09C7\u09A8 \u0995\u09B0\u09C1\u09A8 , \u0986\u09A5\u09AC\u09BE \u09A1\u09BE\u09AF\u09BC\u09BE\u09B2 \u0995\u09B0\u09C1\u09A8 {dial}",
        "",
        f"3. {amount} \u099F\u09BE\u0995\u09BE Send Money \u0995\u09B0\u09C1\u09A8 ",
        "",
        "3. Payment \u09B8\u09AE\u09CD\u09AA\u09A8\u09CD\u09A8 \u09B9\u0993\u09AF\u09BC\u09BE\u09B0 \u09AA\u09B0 TrxID \u099F\u09BE \u0995\u09AA\u09BF \u0995\u09B0\u09C1\u09A8\u0964",
        "",
        "\u26A0\ufe0F Important Notice",
        "",
        "\U0001F539 \u09AD\u09C1\u09B2 \u09A8\u09BE\u09AE\u09CD\u09AC\u09BE\u09B0\u09C7 \u099F\u09BE\u0995\u09BE \u09AA\u09BE\u09A0\u09BE\u09B2\u09C7 \u09A6\u09BE\u09AF\u09BC\u09AD\u09BE\u09B0 \u0986\u09AA\u09A8\u09BE\u09B0",
        "",
        "\U0001F539 TrxID \u099B\u09BE\u09A1\u09BC\u09BE \u09AA\u09C7\u09AE\u09C7\u09A8\u09CD\u099F \u0995\u09A8\u09AB\u09BE\u09B0\u09CD\u09AE \u0995\u09B0\u09BE \u09B9\u09AC\u09C7 \u09A8\u09BE",
        "",
        "\U0001F539 \u09AA\u09C7\u09AE\u09C7\u09A8\u09CD\u099F\u09C7\u09B0 \u0986\u0997\u09C7 \u09A8\u09BE\u09AE\u09CD\u09AC\u09BE\u09B0 \u0993 Amount \u099A\u09C7\u0995 \u0995\u09B0\u09C1\u09A8",
        "",
        "",
        "\u2728 Thank You For Choosing Us \u2728",
        "\U0001F90D \u0986\u09AA\u09A8\u09BE\u09B0 \u09AC\u09BF\u09B6\u09CD\u09AC\u09BE\u09B8\u0987 \u0986\u09AE\u09BE\u09A6\u09C7\u09B0 \u0985\u0997\u09CD\u09B0\u09BE\u09A7\u09BF\u0995\u09BE\u09B0 \U0001F90D",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        "\U0001F338 Fast \u2022 Trusted \u2022 Secure \U0001F338",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    ]
    text = "\n".join(lines)
    amount_anchor = text.find(f"3. {amount} ")
    entities = [e for e in (
        _code_entity(text, number),
        _code_entity(text, amount, max(0, amount_anchor + 3)),
    ) if e]
    return text, entities


async def show_shop_packages(chat_id, context, kind, title):
    packages = get_packages(kind)
    context.user_data["shop_kind"] = kind
    context.user_data["mode"] = f"shop_pick_{kind}"
    if not packages:
        await context.bot.send_message(
            chat_id=chat_id, text="No packages are available right now.",
            reply_markup=shop_menu_markup(),
        )
        return
    rows = [[KeyboardButton(package["label"])] for package in packages]
    rows.append([KeyboardButton("Cancel")])
    await context.bot.send_message(chat_id=chat_id, text=title, reply_markup=green_reply_markup(rows))


def cancel_keyboard():
    """Keyboard that replaces the main menu while a step is waiting for input."""
    return green_reply_markup([[KeyboardButton("Cancel")]])


async def start_favorite_flow(chat_id, context, action):
    """Remove the favorite list/nav and ask for an emoji ID with only Cancel."""
    await delete_emoji_view(chat_id, context)
    context.user_data["mode"] = "favorite_add" if action == "add" else "favorite_remove"
    prompt = ("Send the emoji ID to add to favorite."
              if action == "add" else "Send the emoji ID to remove from favorite.")
    await context.bot.send_message(
        chat_id=chat_id,
        text=prompt,
        reply_markup=favorite_cancel_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    track_user(update)
    context.user_data["uid"] = query.from_user.id
    _lang = get_user_lang(query.from_user.id)
    if _lang:
        CURRENT_LANG.set(_lang)
    if not await ensure_joined(update, context):
        return
    data = query.data or ""
    # /id mode: show the ID of whichever button was tapped.
    if context.user_data.get("mode") == "id_mode":
        await query.message.reply_text(f"🆔 Button ID: {data}")
        return
    if data in ("dummy_page", "dummy_category", "noop"):
        return
    # Orders: approve / reject, user history pagination, admin pending pagination.
    if data.startswith("ord:"):
        if not is_admin(query.from_user.id):
            await query.message.reply_text("\u26d4 This action is available to admins only.")
            return
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        try:
            order_id = int(parts[2])
        except (IndexError, ValueError):
            return
        if action == "approve":
            order = approve_order(order_id)
            if order:
                await notify_order_result(context.bot, order, True)
            await query.edit_message_text(
                f"\u2705 Order #{order_id} approved.\n\n" + order_detail_text(order)
            )
        elif action == "reject":
            order = reject_order(order_id)
            if order:
                await notify_order_result(context.bot, order, False)
            await query.edit_message_text(
                f"\u274c Order #{order_id} rejected.\n\n" + order_detail_text(order)
            )
        return
    if data.startswith("myord:"):
        try:
            page = int(data.split(":")[1])
        except (IndexError, ValueError):
            page = 0
        text, markup = user_orders_page(query.from_user.id, page)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        return
    if data.startswith("admord:"):
        if not is_admin(query.from_user.id):
            return
        try:
            page = int(data.split(":")[1])
        except (IndexError, ValueError):
            page = 0
        text, markup = admin_orders_page(page)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        return
    if data.startswith("prem:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "buy":
            await show_shop_packages(
                query.message.chat_id, context, "premium",
                "\u2b50 Choose a premium package.",
            )
            return
        if action == "activate":
            try:
                order_id = int(parts[2])
            except (IndexError, ValueError):
                return
            order, status = activate_premium_order(order_id)
            if status == "ok":
                await query.edit_message_text(
                    f"\u2705 \u0986\u09AA\u09A8\u09BE\u09B0 \u0985\u09B0\u09CD\u09A1\u09BE\u09B0 \u0995\u09A8\u09AB\u09BE\u09B0\u09CD\u09AE \u09B9\u09AF\u09BC\u09C7\u099B\u09C7!\n\n"
                    f"\U0001F9FE Order #{order['id']}\n"
                    f"\u2b50 Premium activated: {order['amount']} days\n"
                    f"\u23F0 Valid until: {premium_until_text(order['user_id'])}\n\n"
                    "\U0001F60A Enjoy all premium features!"
                )
            elif status == "already":
                await query.edit_message_text(
                    f"\u2705 Premium is already active on your account.\n"
                    f"\u23F0 Valid until: {premium_until_text(order['user_id'])}"
                )
            elif status == "notapproved":
                await query.answer("This order is not approved yet.", show_alert=True)
            else:
                await query.answer("This order cannot be activated.", show_alert=True)
            return
        return
    if data == "favmore":
        await send_premium_prompt(query.message.chat_id, context)
        return
    if data.startswith("eb:"):
        if not is_admin(query.from_user.id):
            await query.message.reply_text("⛔ This action is available to admins only.")
            return
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        try:
            btn_id = int(parts[2])
        except (IndexError, ValueError):
            return
        if action == "name":
            context.user_data["edit_btn_id"] = btn_id
            context.user_data["mode"] = "edit_button_name"
            await query.message.reply_text("Send the new button name.", reply_markup=cancel_keyboard())
        elif action == "emoji":
            context.user_data["edit_btn_id"] = btn_id
            context.user_data["mode"] = "edit_button_emoji"
            await query.message.reply_text("Send the new custom emoji ID.", reply_markup=cancel_keyboard())
        elif action == "color":
            await send_edit_colors(query.message.chat_id, context, btn_id)
        elif action == "setcolor":
            color = parts[3] if len(parts) > 3 else "green"
            with db() as conn:
                conn.execute("UPDATE menu_buttons SET color=? WHERE btn_id=?", (color, btn_id))
            await query.message.reply_text("✅ Button colour updated.")
            await send_edit_button_info(query.message.chat_id, context, btn_id)
        elif action == "pos":
            await send_edit_position(query.message.chat_id, context, btn_id)
        elif action == "move":
            direction = parts[3] if len(parts) > 3 else ""
            if move_menu_button(btn_id, direction):
                await query.message.reply_text("✅ Button moved.")
            else:
                await query.message.reply_text("⚠️ Cannot move further in that direction.")
            await send_edit_button_info(query.message.chat_id, context, btn_id)
        elif action == "done":
            context.user_data["mode"] = None
            await query.message.reply_text("✅ Done.", reply_markup=main_menu_for(update, context))
        return
    if data.startswith("refhist:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await show_refer_history(query.message.chat_id, context, query.from_user.id, page)
        return
    if data.startswith("page:"):
        context.user_data.pop("active_category", None)
        await show_emoji_page(query, context, page=int(data.split(":")[1]), favorites=False)
        await ensure_emoji_keyboard(query.message, context, favorites=False)
    elif data.startswith("fpage:"):
        await show_emoji_page(query, context, page=int(data.split(":")[1]), favorites=True)
        await ensure_emoji_keyboard(query.message, context, favorites=True)
    elif data == "favorites":
        context.user_data.pop("active_category", None)
        context.user_data["mode"] = "make_post"
        await show_emoji_page(query, context, page=0, favorites=True)
        await ensure_emoji_keyboard(query.message, context, favorites=True)
    elif data == "favorite_add":
        await start_favorite_flow(query.message.chat_id, context, "add")
    elif data == "favorite_remove":
        await start_favorite_flow(query.message.chat_id, context, "remove")
    elif data.startswith("ucat:"):
        names = category_names(); idx = int(data.split(":")[1])
        if 0 <= idx < len(names):
            context.user_data["active_category"] = names[idx]
            context.user_data["showing_favorites"] = False
            context.user_data["mode"] = "make_post"
            await show_emoji_page(query, context, page=0, favorites=False)
            await ensure_emoji_keyboard(query.message, context, favorites=False)
    elif data.startswith("acat:"):
        names = category_names(); idx = int(data.split(":")[1])
        if 0 <= idx < len(names):
            await show_category_page(query, context, names[idx], 0, admin=True)
    elif data.startswith("cpage:"):
        _, idx, pg = data.split(":"); names = category_names()
        if 0 <= int(idx) < len(names): await show_category_page(query, context, names[int(idx)], int(pg), admin=True)
    elif data.startswith("ucpage:"):
        _, idx, pg = data.split(":"); names = category_names()
        if 0 <= int(idx) < len(names): await show_category_page(query, context, names[int(idx)], int(pg), admin=False)
    elif data == "category_add":
        if is_admin(query.from_user.id):
            context.user_data["mode"] = "category_add"; await query.message.reply_text("Enter a name for the new category.")
    elif data == "category_manage_add":
        if is_admin(query.from_user.id):
            context.user_data["mode"] = "category_manage_add"
            context.user_data["category_missing_page"] = 0
            await show_category_missing_page(query.message, context, 0)
    elif data.startswith("caddpage:"):
        if is_admin(query.from_user.id):
            page = int(data.split(":", 1)[1])
            context.user_data["category_missing_page"] = page
            category = context.user_data.get("current_category")
            present = {r["emoji_id"] for r in get_category_emojis(category)} if category else set()
            missing = [r for r in get_emojis() if r["emoji_id"] not in present]
            page = max(0, min(page, (max(len(missing)-1, 0)) // PAGE_SIZE))
            subset = missing[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
            header = f"📥 Category: {category} — Available emojis to add\n📄 Page {page+1} · Total: {len(missing)}\n\n"
            text = header; entities = []
            for serial, row in enumerate(subset, start=page*PAGE_SIZE+1):
                alt = row.get("alt") or "😀"
                prefix = f"{serial}. "
                separator = " — "
                shown_id = "{" + str(row["emoji_id"]) + "}"
                offset = utf16_len(text + prefix)
                id_offset = utf16_len(text + prefix + alt + separator)
                text += f"{prefix}{alt}{separator}{shown_id}\n"
                entities.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=offset, length=utf16_len(alt), custom_emoji_id=str(row["emoji_id"])))
                entities.append(MessageEntity(type=MessageEntity.CODE, offset=id_offset, length=utf16_len(shown_id)))
            text += "\nTo add emojis, send an ID, `emoji serial id1`, or a serial range such as `1-5`."
            total_pages = max(1, math.ceil(len(missing) / PAGE_SIZE))
            nav = paged_nav("caddpage", page, total_pages)
            await query.message.edit_text(text, entities=entities, reply_markup=green_inline_markup([nav]))
    elif data == "category_manage_remove":
        if is_admin(query.from_user.id):
            context.user_data["mode"] = "category_manage_remove"; await query.message.reply_text("Enter an ID or serial number to remove from this category.")
    elif data == "category_delete_confirm":
        if is_admin(query.from_user.id):
            category = context.user_data.get("current_category")
            if category:
                keyboard = green_reply_markup([[KeyboardButton("Yes, delete"), KeyboardButton("Cancel")]])
                await query.message.reply_text(f"⚠️ Delete category '{category}' from the database?", reply_markup=keyboard)
    elif data == "category_delete_no":
        await query.edit_message_text("❎ Category deletion cancelled.")
    elif data == "category_delete_yes":
        if is_admin(query.from_user.id):
            category = context.user_data.get("current_category")
            if category:
                with db() as conn:
                    conn.execute("DELETE FROM emoji_categories WHERE category=?", (category,))
                    conn.execute("DELETE FROM categories WHERE name=?", (category,))
                    conn.execute("DELETE FROM menu_buttons WHERE key=? AND scope IN ('category','admin_categories')", (f"cat:{category}",))
                context.user_data.pop("current_category", None)
                await query.edit_message_text(f"✅ Category '{category}' deleted successfully.")
    elif data == "main_make":
        context.user_data.pop("active_category", None)
        context.user_data["mode"] = "make_post"
        await show_emoji_page(query, context, 0, False)
        await ensure_emoji_keyboard(query.message, context, favorites=False)
    elif data == "main_add":
        context.user_data["mode"] = "add_emoji"
        await query.message.reply_text("Send an emoji pack link, a message/forward containing Premium Emoji, or one or more IDs.")
    elif data == "main_remove":
        context.user_data["mode"] = "remove_emoji"
        await query.message.reply_text("Send one or more Emoji IDs to remove them from the database.")
    elif data == "main_view":
        context.user_data["mode"] = "view_emoji"
        await query.message.reply_text("Send an emoji pack link, an Emoji ID, or a message/forward containing Premium Emoji.")


async def add_pack(message, pack_name, context):
    """Fetch pack, validate in batches of 10, save only IDs Telegram preserves as custom emoji."""
    status = await message.reply_text("⏳ Loading emoji pack…\nPlease wait.")
    started = time.monotonic()
    try:
        result = await user_client(GetStickerSetRequest(
            stickerset=InputStickerSetShortName(short_name=pack_name), hash=0
        ))
        docs = list(result.documents)
        total = len(docs)
        added = duplicate = failed = checked = 0
        # Build batches of 10. A temporary test message is deleted immediately after Telegram validates it.
        for offset in range(0, total, 10):
            batch = docs[offset:offset + 10]
            test_text = ""; test_entities = []; expected = []
            for doc in batch:
                alt = get_custom_emoji_alt(doc) or "😀"
                pos = utf16_len(test_text)
                test_text += alt
                test_entities.append(MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI, offset=pos,
                    length=utf16_len(alt), custom_emoji_id=str(doc.id)
                ))
                expected.append((str(doc.id), alt))
            valid_ids = set()
            try:
                probe = await context.bot.send_message(
                    chat_id=message.chat_id, text=test_text, entities=test_entities,
                    disable_notification=True
                )
                returned = probe.entities or []
                valid_ids = {str(e.custom_emoji_id) for e in returned
                             if e.type == MessageEntity.CUSTOM_EMOJI and getattr(e, "custom_emoji_id", None)}
                try:
                    await context.bot.delete_message(chat_id=message.chat_id, message_id=probe.message_id)
                except TelegramError:
                    pass
            except TelegramError as exc:
                print("Emoji batch validation error:", repr(exc))
                valid_ids = set()
            for emoji_id, alt in expected:
                checked += 1
                if emoji_id not in valid_ids:
                    failed += 1
                elif save_emoji(emoji_id, alt, result.set.title):
                    added += 1
                else:
                    duplicate += 1
            elapsed = max(time.monotonic() - started, 0.01)
            rate = checked / elapsed
            remaining = max(total - checked, 0)
            eta = int(remaining / rate) if rate else 0
            anim = ["▱▱▱▱▱", "▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰"]
            bar = anim[(checked // 10) % len(anim)]
            try:
                await status.edit_text(
                    f"📦 {result.set.title}\nTotal emojis: {total}\n"
                    f"Added to database: {added}\nChecked: {checked}/{total}\n"
                    f"{bar} Checking…\nEstimated time remaining: {eta} seconds"
                )
            except TelegramError:
                pass
        elapsed = int(time.monotonic() - started)
        await status.edit_text(
            f"✅ Emoji pack check completed: {result.set.title}\n\n"
            f"📦 Total emojis: {total}\n"
            f"💾 Newly saved: {added}\n"
            f"♻️ Duplicates: {duplicate}\n"
            f"⚠️ Failed to render or validate (not saved): {failed}\n"
            f"⏱️ Total time: {elapsed} seconds"
        )
    except Exception as exc:
        print("Pack error:", repr(exc))
        await status.edit_text("❌ Could not read the pack. Please check that the link is valid.")


def entity_to_dict(entity, offset=None, length=None):
    """Serialize Telegram text entity while retaining formatting/link metadata."""
    item = {
        'type': entity.type,
        'offset': entity.offset if offset is None else offset,
        'length': entity.length if length is None else length,
    }
    for key in ('url', 'user', 'language', 'custom_emoji_id'):
        value = getattr(entity, key, None)
        if value is not None:
            item[key] = value
    return item


def make_message_entity(item):
    """Rebuild a python-telegram-bot MessageEntity from stored fields."""
    kwargs = {k: item[k] for k in ('url', 'user', 'language', 'custom_emoji_id') if item.get(k) is not None}
    return MessageEntity(type=item['type'], offset=item['offset'], length=item['length'], **kwargs)


def resolve_emoji_text(raw_text, incoming_entities=None):
    """Resolve {emoji_id}/{serial} tokens and preserve Telegram formatting entities.

    Returns (output_text, stored_entities) where stored_entities is a list of
    serialisable dicts ready to be rebuilt with make_message_entity().
    """
    incoming_entities = incoming_entities or []
    with db() as conn:
        all_rows = [dict(r) for r in conn.execute(
            "SELECT emoji_id, alt FROM emojis ORDER BY sort_order, added_at, emoji_id"
        ).fetchall()]
        cats = conn.execute("SELECT name, category_id FROM categories ORDER BY category_id").fetchall()
        by_id = {str(r['emoji_id']): r for r in all_rows}

    pat = re.compile(
        r"\{\s*cat\s*(\d+)\s+emoji\s+serial\s+id\s*(\d+)\s*\}"
        r"|\{\s*serial\s+id\s*(\d+)\s*\}"
        r"|\{\s*(\d{5,30})\s*\}"
        r"|\{\s*(\d{1,4}(?:\s*,\s*\d{1,4})*)\s*\}"
    )
    replacements = []
    for m in pat.finditer(raw_text):
        ids = []
        if m.group(1):
            cat_id, serial = int(m.group(1)), int(m.group(2))
            catrow = next((c for c in cats if int(c['category_id'] or 0) == cat_id), None)
            if catrow:
                cr_ids = {str(r['emoji_id']) for r in get_category_emojis(catrow['name'])}
                if 1 <= serial <= len(all_rows) and str(all_rows[serial-1]['emoji_id']) in cr_ids:
                    ids = [str(all_rows[serial-1]['emoji_id'])]
        elif m.group(3):
            serial = int(m.group(3))
            if 1 <= serial <= len(all_rows):
                ids = [str(all_rows[serial-1]['emoji_id'])]
        elif m.group(4):
            if m.group(4) in by_id:
                ids = [m.group(4)]
        else:
            for n in map(int, m.group(5).split(',')):
                if 1 <= n <= len(all_rows):
                    ids.append(str(all_rows[n-1]['emoji_id']))
        if ids:
            replacements.append((m.start(), m.end(), ids))

    # Rebuild text and record where each original character boundary moved to.
    out_parts = []
    new_custom_entities = []
    cursor = 0
    output_length = 0  # UTF-16 code units, as required by Telegram entities
    mapped_spans = []  # (source_begin, source_end, output_begin, output_end)
    for begin, finish, ids in replacements:
        prefix = raw_text[cursor:begin]
        out_parts.append(prefix)
        output_length += utf16_len(prefix)
        output_begin = output_length
        for eid in ids:
            row = by_id.get(eid)
            if not row:
                continue
            alt = row.get('alt') or '😀'
            new_custom_entities.append(MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI,
                offset=output_length,
                length=utf16_len(alt),
                custom_emoji_id=eid,
            ))
            out_parts.append(alt)
            output_length += utf16_len(alt)
        mapped_spans.append((begin, finish, output_begin, output_length))
        cursor = finish
    tail = raw_text[cursor:]
    out_parts.append(tail)
    out = ''.join(out_parts)

    def source_boundary_to_output(source_offset, is_end=False):
        # Telegram entity offsets are UTF-16; convert the source boundary to a Python index.
        consumed = 0
        for i, ch in enumerate(raw_text):
            width = utf16_len(ch)
            if consumed == source_offset:
                break
            if consumed < source_offset < consumed + width:
                break
            consumed += width

        delta = 0
        for begin, finish, out_begin, out_end in mapped_spans:
            original_begin = utf16_len(raw_text[:begin])
            original_end = utf16_len(raw_text[:finish])
            if source_offset <= original_begin:
                break
            if source_offset < original_end:
                return out_end if is_end else out_begin
            delta += (out_end - out_begin) - (original_end - original_begin)
        return source_offset + delta

    # Preserve bold, italic, underline, spoiler, quote, links, code, strikethrough,
    # blockquote and any other Telegram entities supplied with the incoming message.
    stored_entities = []
    for entity in incoming_entities:
        new_offset = source_boundary_to_output(entity.offset, is_end=False)
        new_end = source_boundary_to_output(entity.offset + entity.length, is_end=True)
        if new_end > new_offset:
            stored_entities.append(entity_to_dict(entity, new_offset, new_end - new_offset))

    stored_entities.extend(entity_to_dict(e) for e in new_custom_entities)
    stored_entities.sort(key=lambda e: (e['offset'], -e['length']))
    return out, stored_entities


async def begin_post_from_text(message, raw_text, context):
    """Build a draft from the user's text, append the watermark and start the flow."""
    incoming = getattr(message, 'entities', None) or getattr(message, 'caption_entities', None) or []
    out, stored_entities = resolve_emoji_text(raw_text, incoming)
    if not out.strip():
        return False

    bot_username = context.bot.username or ""
    watermark = f"# This post was make @{bot_username}" if bot_username else "# This post was make"
    full_text = out + "\n\n" + watermark
    entities = list(stored_entities)
    entities.append({
        'type': 'text_link',
        'offset': utf16_len(out + "\n\n"),
        'length': utf16_len(watermark),
        'url': f"https://t.me/{bot_username}" if bot_username else "https://t.me",
    })
    entities.sort(key=lambda e: (e['offset'], -e['length']))

    context.user_data['post_draft'] = {
        'base_text': out,
        'base_entities': stored_entities,
        'text': full_text,
        'entities': entities,
        'watermark': True,
        'photos': [],
        'buttons': [],
        'buttons_per_line': 2,
    }
    context.user_data['post_adding_more'] = False
    await delete_post_step_messages(message.chat_id, context, include_emoji_list=True)
    await show_generating(message.chat_id, context)
    await post_start_image_step(message.chat_id, context)
    return True


def draft_entities(draft):
    return [make_message_entity(e) for e in draft.get('entities', [])]


def post_button_keyboard(draft):
    """Build coloured inline buttons, honouring buttons_per_line."""
    if isinstance(draft, dict):
        buttons = draft.get("buttons", [])
        try:
            per_line = max(1, int(draft.get("buttons_per_line", 2)))
        except (TypeError, ValueError):
            per_line = 2
    else:
        buttons = draft or []
        per_line = 2
    rows = []
    for i in range(0, len(buttons), per_line):
        row = []
        for button in buttons[i:i + per_line]:
            item = {"text": button.get("name", "Button"), "url": button.get("url", "https://t.me")}
            style = color_style(button.get("color", "normal"))
            if style:
                item["style"] = style
            row.append(item)
        rows.append(row)
    return {"inline_keyboard": rows} if rows else None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    track_user(update)

    context.user_data["uid"] = update.effective_user.id
    _lang = get_user_lang(update.effective_user.id)
    if _lang:
        CURRENT_LANG.set(_lang)
    menu = unL((message.text or "").strip())
    for _prefix in ("\u2705 ", "\U0001F381 ", "\U0001F48E ", "\u2B50 "):
        if menu.startswith(_prefix):
            menu = unL(menu[len(_prefix):].strip())
            break
    text = message.text or message.caption or ""

    # Channel-join gate.
    if unL(menu) in {"✅ Check join", "Check join"}:
        if await ensure_joined(update, context):
            await continue_onboarding(update, context)
        return
    if not await ensure_joined(update, context):
        return

    mode = context.user_data.get("mode", "")

    # /id mode: report the ID of whichever button on the current menu is tapped.
    if mode == "id_mode":
        menu_context = context.user_data.get("id_menu_context", "main")
        return_mode = context.user_data.get("id_return_mode")
        if menu == "Cancel":
            context.user_data["mode"] = return_mode
            await message.reply_text(
                "Exited ID mode.",
                reply_markup=menu_markup_for_context(context, menu_context),
            )
            return
        ident, name = resolve_button_identifier(menu, menu_context)
        context.user_data["mode"] = return_mode
        await message.reply_text(
            f"🆔 Button ID: {ident}\nName: {name}",
            reply_markup=menu_markup_for_context(context, menu_context),
        )
        return

    # Language selection and the first-time premium offer.
    lang_choice = LANG_LABEL_TO_CODE.get(menu)
    if lang_choice:
        set_user_lang(update.effective_user.id, lang_choice)
        CURRENT_LANG.set(lang_choice)
        context.user_data["mode"] = None
        await message.reply_text(
            "\u2705 " + L("Language") + ": " + LANG_NAMES[lang_choice]
        )
        await continue_onboarding(update, context)
        return
    if menu == "Language":
        await send_language_prompt(message.chat_id, context)
        return
    if menu in {"Claim 3 days premium", "Skip"}:
        if menu == "Skip":
            mark_onboarded(update.effective_user.id)
            context.user_data["mode"] = None
            await message.reply_text(
                "\U0001F44B " + L("Welcome to Premium Emoji Bot\nPlease choose an option from the menu below."),
                reply_markup=main_menu_for(update, context),
            )
            return
        with db() as conn:
            row = conn.execute(
                "SELECT premium_claimed FROM users WHERE user_id=?",
                (update.effective_user.id,),
            ).fetchone()
        if row and int(row["premium_claimed"] or 0):
            mark_onboarded(update.effective_user.id)
            await message.reply_text(
                L("You have already claimed your free premium."),
                reply_markup=main_menu_for(update, context),
            )
            return
        await send_claim_prompt(message.chat_id, context, update.effective_user.id)
        return

    # Cancel button for the favorite add/remove flows.
    if menu == "Cancel" and mode in {"favorite_add", "favorite_remove"}:
        context.user_data["mode"] = "make_post"
        await show_emoji_page(update, context, 0, favorites=True)
        await ensure_emoji_keyboard(message, context, favorites=True)
        return
    if menu == "Cancel" and not (mode or "").startswith(("post_", "shop_", "gen_redeem_", "edit_button_")) and not (
            context.user_data.get("current_category")
            and mode in {"admin_categories", "category_manage_add", "category_manage_remove"}):
        context.user_data["mode"] = None
        await message.reply_text("Cancelled.", reply_markup=main_menu_for(update, context))
        return

    # /edit_button: select a button on the current menu, then edit it.
    if mode == "edit_button_select":
        scope = context.user_data.get("edit_menu_context", "main")
        if menu == "Cancel":
            context.user_data["mode"] = context.user_data.get("edit_return_mode")
            await message.reply_text(
                "Cancelled.", reply_markup=menu_markup_for_context(context, scope)
            )
            return
        btn = menu_button_by_text(menu, scope)
        if not btn and menu.startswith("\u2705 "):
            btn = menu_button_by_text(menu[2:].strip(), scope)
        if not btn:
            await message.reply_text(
                "Please select one of the buttons shown below.",
                reply_markup=menu_markup_for_context(context, scope),
            )
            return
        context.user_data["edit_btn_id"] = btn["btn_id"]
        context.user_data["mode"] = "edit_button_menu"
        await send_edit_button_info(message.chat_id, context, btn["btn_id"])
        return

    if mode == "edit_button_name":
        if menu == "Cancel":
            context.user_data["mode"] = "edit_button_menu"
            scope = context.user_data.get("edit_menu_context", "main")
            await message.reply_text(
                "Cancelled.", reply_markup=menu_markup_for_context(context, scope)
            )
            return
        new_name = (message.text or "").strip()[:64]
        if not new_name:
            await message.reply_text(
                "Please send the new button name.",
                reply_markup=cancel_keyboard(),
            )
            return
        btn_id = context.user_data.get("edit_btn_id")
        with db() as conn:
            conn.execute("UPDATE menu_buttons SET text=? WHERE btn_id=?", (new_name, int(btn_id)))
        context.user_data["mode"] = "edit_button_menu"
        await message.reply_text("✅ Button name updated.", reply_markup=main_menu_for(update, context))
        await send_edit_button_info(message.chat_id, context, btn_id)
        return

    if mode == "edit_button_emoji":
        if menu == "Cancel":
            context.user_data["mode"] = "edit_button_menu"
            scope = context.user_data.get("edit_menu_context", "main")
            await message.reply_text(
                "Cancelled.", reply_markup=menu_markup_for_context(context, scope)
            )
            return
        emoji_id = (message.text or "").strip().strip("{}()[]")
        if not re.fullmatch(r"\d{5,30}", emoji_id):
            await message.reply_text(
                "❌ Please send a valid custom emoji ID (digits only).",
                reply_markup=cancel_keyboard(),
            )
            return
        btn_id = context.user_data.get("edit_btn_id")
        with db() as conn:
            conn.execute("UPDATE menu_buttons SET emoji_id=? WHERE btn_id=?", (emoji_id, int(btn_id)))
        context.user_data["mode"] = "edit_button_menu"
        await message.reply_text("✅ Button emoji updated.", reply_markup=main_menu_for(update, context))
        await send_edit_button_info(message.chat_id, context, btn_id)
        return

    # Back button: favorite menu -> category menu; category menu -> main menu.
    if menu == "Back":
        if mode == "bot_settings":
            context.user_data["mode"] = None
            await message.reply_text("Main menu", reply_markup=main_menu_for(update, context))
            return
        if mode in {"favorite_add", "favorite_remove"}:
            context.user_data["mode"] = "make_post"
            await show_emoji_page(update, context, 0, favorites=True)
            await ensure_emoji_keyboard(message, context, favorites=True)
            return
        if context.user_data.get("emoji_keyboard_mode") == "favorites":
            # From the Favorite menu: category menu with All emoji shown.
            context.user_data.pop("active_category", None)
            context.user_data["mode"] = "make_post"
            await show_emoji_page(update, context, 0, False)
            await ensure_emoji_keyboard(message, context, favorites=False)
            return
        # From the category menu: delete the list and return to the main menu.
        await show_welcome(message.chat_id, context)
        return

    # Main menu
    if menu == "Make a post":
        context.user_data.pop("active_category", None)
        context.user_data["emoji_list_message_ids"] = []
        context.user_data["post_preview_message_ids"] = []
        context.user_data["mode"] = "make_post"
        await show_emoji_page(update, context, 0, False)
        await ensure_emoji_keyboard(message, context, favorites=False, force=True)
        return
    if menu == "Add emoji":
        context.user_data["mode"] = "add_emoji"
        await message.reply_text(
            "Send an emoji pack link, a message/forward containing Premium Emoji, or an ID.",
            reply_markup=cancel_keyboard(),
        )
        return
    if menu == "Bot settings":
        if not is_admin(update.effective_user.id):
            await message.reply_text(
                "⚙️ Bot settings\n\n"
                "• View emoji ID — inspect any emoji ID or pack.\n"
                "• Account — your profile, balance and favorites.\n"
                "• Tutorial — a quick guide to the bot.\n"
                "• Support — contact the admin.\n\n"
                "Admin tools are available to the bot owner only.",
                reply_markup=green_reply_markup([[KeyboardButton("Back")]]),
            )
            return
        context.user_data["mode"] = "bot_settings"
        await message.reply_text(
            "⚙️ Bot settings\n\nChoose a tool below.",
            reply_markup=bot_settings_keyboard(),
        )
        return
    if menu == "Remove emoji":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ This option is available to admins only."); return
        context.user_data["mode"] = "remove_emoji"
        await message.reply_text(
            "Send an ID, `emoji serial id1`, `emoji serial id2`, or a serial range such as `emoji serial id5-10`.",
            reply_markup=cancel_keyboard(),
        )
        return
    if menu in {"📂 Category", "📁 Category"}:
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        context.user_data["mode"] = "admin_categories"
        await message.reply_text(
            "📂 Categories — choose a category from the keyboard below.",
            reply_markup=admin_categories_keyboard(),
        )
        return
    if menu == "View emoji ID":
        context.user_data["mode"] = "view_emoji"
        await message.reply_text(
            "Send an emoji pack link, an Emoji ID, or a message/forward containing Premium Emoji.",
            reply_markup=cancel_keyboard(),
        )
        return

    # Emoji-list reply-keyboard selectors (Make a post).
    if menu == "All emoji":
        context.user_data["mode"] = "make_post"
        context.user_data.pop("active_category", None)
        await show_emoji_page(update, context, 0, False)
        await ensure_emoji_keyboard(message, context, favorites=False)
        return
    if menu == "Favorite":
        context.user_data["mode"] = "make_post"
        context.user_data.pop("active_category", None)
        await show_emoji_page(update, context, 0, True)
        await ensure_emoji_keyboard(message, context, favorites=True, force=True)
        return
    if menu == "Add favorite":
        await start_favorite_flow(message.chat_id, context, "add")
        return
    if menu == "Remove favorite":
        await start_favorite_flow(message.chat_id, context, "remove")
        return
    if menu in category_names() and context.user_data.get("mode") in {"make_post"}:
        context.user_data["active_category"] = menu
        context.user_data["mode"] = "make_post"
        context.user_data["showing_favorites"] = False
        await show_emoji_page(update, context, 0, False)
        await ensure_emoji_keyboard(message, context, favorites=False)
        return

    # Account / My order / Admin orders / Refer / Task / Shop / Redeem.
    if menu == "My order":
        context.user_data["mode"] = "my_order"
        await show_my_orders(message.chat_id, context, update.effective_user.id, 0)
        return
    if menu == "Order":
        if not is_admin(update.effective_user.id):
            await message.reply_text("\u26d4 This option is available to admins only.")
            return
        context.user_data["mode"] = "admin_orders"
        await show_admin_orders(message.chat_id, context, 0)
        return
    if menu == "Account":
        uid = update.effective_user.id
        user = update.effective_user
        name = user.first_name or "\u2014"
        username = user.username
        premium = is_premium(uid)
        dash = "\u2014"
        credit = user_balance(uid)
        until = premium_until_text(uid)
        lang = CURRENT_LANG.get()
        words = ACCOUNT_WORDS.get(lang, ACCOUNT_WORDS["en"])
        status = words["premium"] if premium else words["normal"]
        if premium:
            expiry_line = f"{words['valid']}: {until}"
        elif until != dash:
            expiry_line = f"{words['expired']}: {until}"
        else:
            expiry_line = words["no_premium"]
        uname = "@" + username if username else dash
        await message.reply_text(
            f"\U0001F464 {words['title']}\n\n"
            f"{words['name']}: {name}\n"
            f"{words['username']}: {uname}\n"
            f"{words['user_id']}: {uid}\n"
            f"{words['status']}: {status}\n"
            f"{expiry_line}\n"
            f"{words['credit']}: {credit}\n"
            f"{words['language']}: {LANG_NAMES.get(lang, LANG_NAMES['en'])}",
            reply_markup=green_reply_markup([[KeyboardButton("Back to main")]]),
        )
        return
    if menu == "Refer":
        uid = update.effective_user.id
        bot_username = getattr(context.bot, "username", None)
        link = f"https://t.me/{bot_username}?start=ref{uid}" if bot_username else "—"
        with db() as conn:
            refs = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        context.user_data["mode"] = "refer"
        await message.reply_text(
            "🎁 Refer & Earn\n\n"
            "Invite friends with your personal link. You earn 5 coins for every new user.\n\n"
            f"Your link:\n{link}\n\n"
            f"Total referrals: {refs}",
            reply_markup=refer_menu_markup(),
        )
        return
    if menu == "Refer history":
        await show_refer_history(message.chat_id, context, update.effective_user.id, 0)
        return
    if menu == "Leader board":
        await show_refer_leaderboard(message.chat_id, context)
        return
    if menu == "Back to main":
        context.user_data["mode"] = None
        await message.reply_text("Main menu", reply_markup=main_menu_for(update, context))
        return
    if menu == "Task":
        context.user_data["mode"] = "task"
        await message.reply_text(
            "🎯 Tasks\n\n"
            "• Daily check-in — +10 coins (once every 24 hours)\n"
            "• Invite a friend — +5 coins each (see Refer)\n\n"
            "Tap “Claim daily reward” to collect your daily bonus.",
            reply_markup=task_menu_markup(),
        )
        return
    if menu == "Claim daily reward":
        uid = update.effective_user.id
        now = datetime.datetime.utcnow()
        with db() as conn:
            row = conn.execute("SELECT claimed_at FROM task_claims WHERE user_id=? AND task='daily'", (uid,)).fetchone()
        if row:
            try:
                last = datetime.datetime.fromisoformat(str(row["claimed_at"]))
            except ValueError:
                last = None
            if last and (now - last).total_seconds() < 86400:
                remaining = 86400 - int((now - last).total_seconds())
                await message.reply_text(
                    f"⏳ Already claimed. Try again in {remaining // 3600}h {(remaining % 3600) // 60}m.",
                    reply_markup=green_reply_markup([[KeyboardButton("Back")]]),
                )
                return
        with db() as conn:
            conn.execute(
                "INSERT INTO task_claims(user_id, task, claimed_at) VALUES(?, 'daily', CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id, task) DO UPDATE SET claimed_at=CURRENT_TIMESTAMP",
                (uid,),
            )
        balance = add_balance(uid, 10)
        await message.reply_text(
            f"✅ Daily reward claimed: +10 coins\n💰 Balance: {balance} coins",
            reply_markup=green_reply_markup([[KeyboardButton("Back")]]),
        )
        return
    if menu == "Shop":
        context.user_data["mode"] = "shop"
        await message.reply_text(
            "🛍️ Shop\n\nChoose what you want to buy.",
            reply_markup=shop_menu_markup(),
        )
        return
    if mode == "shop":
        if menu == "Buy credit":
            await show_shop_packages(message.chat_id, context, "credit", "💳 Choose a credit package.")
            return
        if menu == "Buy premium":
            await show_shop_packages(message.chat_id, context, "premium", "⭐ Choose a premium package.")
            return
        if menu == "Buy FF like":
            await show_shop_packages(message.chat_id, context, "ff", "❤️ Choose an FF auto-like package.")
            return
        if menu == "Generate redeem code":
            context.user_data["mode"] = "gen_redeem_users"
            await message.reply_text(
                "🎟️ Generate redeem code\n\nHow many users can redeem this code?",
                reply_markup=cancel_keyboard(),
            )
            return
        if menu == "Back to main":
            context.user_data["mode"] = None
            await message.reply_text("Main menu", reply_markup=main_menu_for(update, context))
            return
        return
    if mode in ("shop_pick_credit", "shop_pick_premium", "shop_pick_ff"):
        kind = mode.replace("shop_pick_", "")
        if menu == "Cancel":
            context.user_data["mode"] = "shop"
            await message.reply_text("🛍️ Shop", reply_markup=shop_menu_markup())
            return
        packages = get_packages(kind)
        package = next((p for p in packages if p["label"] == menu), None)
        if not package:
            rows = [[KeyboardButton(p["label"])] for p in packages]
            rows.append([KeyboardButton("Cancel")])
            await message.reply_text(
                "Please choose one of the packages below.",
                reply_markup=green_reply_markup(rows),
            )
            return
        context.user_data["pending_order"] = {"kind": kind, "package": package, "method": ""}
        context.user_data["mode"] = "shop_payment"
        await message.reply_text(
            f"💳 {package['label']} — {package['price']} taka\n\nChoose a payment method.",
            reply_markup=payment_method_markup(),
        )
        return
    if mode in ("shop_payment", "shop_trx"):
        pending = context.user_data.get("pending_order")
        if not pending:
            context.user_data["mode"] = "shop"
            await message.reply_text(
                "Please start again from Shop.", reply_markup=shop_menu_markup()
            )
            return
        clean = menu.replace("\u2705", "").strip()
        if clean in ("Back to package", "Back", "Cancel"):
            context.user_data.pop("pending_order", None)
            context.user_data.pop("payment_selected", None)
            await show_shop_packages(
                message.chat_id, context, pending["kind"], "\U0001F4B3 Choose a package."
            )
            return
        if clean in ("Bikash", "Nagad"):
            pending["method"] = clean
            pending["awaiting_trx"] = True
            context.user_data["payment_selected"] = clean
            context.user_data["mode"] = "shop_payment"
            info, entities = build_payment_info(pending["package"], clean)
            await message.reply_text(
                info, entities=entities, reply_markup=payment_method_markup(clean)
            )
            await message.reply_text(
                "\U0001F9FE \u098F\u0996\u09A8 \u0986\u09AA\u09A8\u09BE\u09B0 TrxID \u09A6\u09BF\u09A8 "
                "(\u0985\u09A5\u09AC\u09BE \u09AA\u09C7\u09AE\u09C7\u09A8\u09CD\u099F\u09C7\u09B0 \u09B8\u09CD\u0995\u09CD\u09B0\u09BF\u09A8\u09B6\u099F \u09AA\u09BE\u09A0\u09BE\u09A8)\u0964",
                reply_markup=payment_method_markup(clean),
            )
            return
        if clean == "Other":
            whatsapp = get_setting("whatsapp_link")
            telegram = get_setting("telegram_link")
            rows = []
            if whatsapp:
                rows.append([{"text": "WhatsApp", "url": whatsapp}])
            if telegram:
                rows.append([{"text": "Telegram", "url": telegram}])
            await message.reply_text(
                "\U0001F4AC Contact the admin on WhatsApp or Telegram to complete your payment.",
                reply_markup={"inline_keyboard": rows} if rows else None,
            )
            context.user_data["mode"] = "shop"
            context.user_data.pop("payment_selected", None)
            return
        if pending.get("awaiting_trx"):
            trx = ""
            file_id = ""
            if message.photo:
                file_id = message.photo[-1].file_id
            else:
                trx = text.strip()
                if not trx:
                    await message.reply_text(
                        "TrxID \u09A6\u09BF\u09A8 \u0985\u09A5\u09AC\u09BE \u09B8\u09CD\u0995\u09CD\u09B0\u09BF\u09A8\u09B6\u099F \u09AA\u09BE\u09A0\u09BE\u09A8\u0964",
                        reply_markup=payment_method_markup(pending.get("method")),
                    )
                    return
            order_id = create_order(
                update.effective_user.id, pending["kind"], pending["package"],
                pending.get("method", ""), trx, file_id,
            )
            context.user_data.pop("pending_order", None)
            context.user_data.pop("payment_selected", None)
            context.user_data["mode"] = None
            await send_order_to_admin(context.bot, order_id)
            await message.reply_text(
                "\u2705 \u0986\u09AA\u09A8\u09BE\u09B0 \u0985\u09B0\u09CD\u09A1\u09BE\u09B0 \u099C\u09AE\u09BE \u09B9\u09AF\u09BC\u09C7\u099B\u09C7!\n"
                f"\U0001F9FE Order ID: #{order_id}\n"
                "\u23F3 Status: Pending\n\n"
                "\u0985\u09CD\u09AF\u09BE\u09A1\u09AE\u09BF\u09A8 \u09AF\u09BE\u099A\u09BE\u0987 \u0995\u09B0\u09C7 \u0985\u09AA\u09CD\u09B0\u09C1\u09AD \u0995\u09B0\u09B2\u09C7 \u0986\u09AA\u09A8\u09BE\u0995\u09C7 \u099C\u09BE\u09A8\u09BE\u09A8\u09CB \u09B9\u09AC\u09C7\u0964",
                reply_markup=main_menu_for(update, context),
            )
            return
        await message.reply_text(
            "Please choose Bikash, Nagad, Other, or Back to package.",
            reply_markup=payment_method_markup(context.user_data.get("payment_selected")),
        )
        return
    if mode == "gen_redeem_users":
        if menu == "Cancel":
            context.user_data["mode"] = "shop"
            await message.reply_text("🛍️ Shop", reply_markup=shop_menu_markup())
            return
        if not text.strip().isdigit() or int(text.strip()) < 1:
            await message.reply_text(
                "Please send how many users can redeem the code (a number).",
                reply_markup=cancel_keyboard(),
            )
            return
        context.user_data["gen_redeem_users"] = int(text.strip())
        context.user_data["mode"] = "gen_redeem_credits"
        await message.reply_text(
            "How many credits will each user get?",
            reply_markup=cancel_keyboard(),
        )
        return
    if mode == "gen_redeem_credits":
        if menu == "Cancel":
            context.user_data["mode"] = "shop"
            await message.reply_text("🛍️ Shop", reply_markup=shop_menu_markup())
            return
        if not text.strip().isdigit() or int(text.strip()) < 1:
            await message.reply_text(
                "Please send how many credits each user gets (a number).",
                reply_markup=cancel_keyboard(),
            )
            return
        users = int(context.user_data.get("gen_redeem_users", 1))
        credits = int(text.strip())
        fee = int(get_setting("redeem_fee", "1") or 1)
        cost = users * credits + fee
        uid = update.effective_user.id
        balance = user_balance(uid)
        if balance < cost:
            context.user_data["mode"] = "shop"
            await message.reply_text(
                f"❌ Insufficient balance.\n\nThis code costs {cost} credits "
                f"({users} × {credits} + {fee} fee).\nYour balance: {balance} credits.",
                reply_markup=shop_menu_markup(),
            )
            return
        add_balance(uid, -cost)
        code = create_redeem_code(credits, users, uid)
        context.user_data["mode"] = "shop"
        await message.reply_text(
            "✅ Redeem code generated!\n\n"
            f"Code: {code}\n"
            f"Users: {users}\n"
            f"Credits each: {credits}\n"
            f"Cost: {cost} credits\n"
            f"New balance: {user_balance(uid)} credits",
            reply_markup=shop_menu_markup(),
        )
        return
    if menu == "Create redeem code":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ This option is available to admins only."); return
        context.user_data["mode"] = "redeem_create_users"
        await message.reply_text(
            "🎟️ Create redeem code\n\nHow many users can use this code?",
            reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
        )
        return
    if menu == "Redeem code":
        context.user_data["mode"] = "redeem_use"
        await message.reply_text(
            "🎟️ Redeem code\n\nSend the code you want to redeem.",
            reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
        )
        return
    if menu == "Tutorial":
        await message.reply_text(
            "📖 Tutorial\n\n"
            "1. Tap “Make a post” to open the category menu.\n"
            "2. Pick “All emoji” or any category to browse emoji IDs.\n"
            "3. Use Previous / Next to move between pages.\n"
            "4. Type your message and insert an emoji with {emoji_id} or a serial id.\n"
            "5. Tap “Favorite” to keep a personal list of emoji IDs.\n\n"
            "Need help? Open Support.",
            reply_markup=green_reply_markup([[KeyboardButton("Back")]]),
        )
        return
    if menu == "Support":
        await message.reply_text(
            "🛟 Support\n\n"
            "If you need help, contact the bot owner or admin.\n"
            "Describe your issue and include your User ID from the Account page.",
            reply_markup=green_reply_markup([[KeyboardButton("Back")]]),
        )
        return

    # Admin category-management reply-keyboard actions.
    if menu == "Create category":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        context.user_data["mode"] = "category_add"
        await message.reply_text("Enter a name for the new category.")
        return
    if menu == "Back to menu":
        context.user_data.pop("mode", None)
        context.user_data.pop("current_category", None)
        await message.reply_text("Main menu", reply_markup=main_menu_for(update, context))
        return
    if menu == "Back to categories":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        context.user_data["mode"] = "admin_categories"
        await message.reply_text("📂 Categories", reply_markup=admin_categories_keyboard())
        return
    if menu == "Add to category":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        if not context.user_data.get("current_category"):
            await message.reply_text("No category selected.")
            return
        context.user_data["mode"] = "category_manage_add"
        context.user_data["category_missing_page"] = 0
        await show_category_missing_page(message, context, 0)
        return
    if menu == "Remove from category":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        context.user_data["mode"] = "category_manage_remove"
        await message.reply_text("Enter an ID or serial number to remove from this category.")
        return
    if menu == "Delete category":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        category = context.user_data.get("current_category")
        if category:
            await message.reply_text(
                f"⚠️ Delete category '{category}' from the database?",
                reply_markup=green_reply_markup([[KeyboardButton("Yes, delete"), KeyboardButton("Cancel")]]),
            )
        return
    if menu in {"Yes, delete", "Cancel"} and context.user_data.get("current_category"):
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ Category management is available to admins only."); return
        category = context.user_data.get("current_category")
        if menu == "Yes, delete":
            with db() as conn:
                conn.execute("DELETE FROM emoji_categories WHERE category=?", (category,))
                conn.execute("DELETE FROM categories WHERE name=?", (category,))
                conn.execute("DELETE FROM menu_buttons WHERE key=? AND scope IN ('category','admin_categories')", (f"cat:{category}",))
            context.user_data.pop("current_category", None)
            context.user_data["mode"] = "admin_categories"
            await message.reply_text(
                f"✅ Category '{category}' deleted successfully.",
                reply_markup=admin_categories_keyboard(),
            )
        else:
            await message.reply_text("❌ Category deletion cancelled.", reply_markup=admin_category_page_keyboard())
        return

    # Admin category selection from the reply keyboard.
    if (is_admin(update.effective_user.id)
            and menu in category_names()
            and context.user_data.get("mode") == "admin_categories"):
        await show_category_page(update, context, menu, 0, admin=True)
        return

    text = message.text or message.caption or ""
    pack_name = get_pack_name(text)
    mode = context.user_data.get("mode", "")

    # ------------------------------------------------------------------
    # Post builder state machine.
    # ------------------------------------------------------------------
    draft = context.user_data.get("post_draft")

    if mode == "post_wait_image":
        if menu == "Skip":
            await post_go_to_button_name(message.chat_id, context)
            return
        if message.photo and draft is not None:
            await delete_post_step_messages(message.chat_id, context)
            draft["photos"] = [message.photo[-1].file_id]
            await show_generating(message.chat_id, context)
            await post_image_review_step(message.chat_id, context)
            return
        await message.reply_text(
            "📷 Please send a photo, or tap Skip.",
            reply_markup=post_step_keyboard([["Skip"]]),
        )
        return

    if mode == "post_replace_image":
        if menu == "Cancel":
            await post_image_review_step(message.chat_id, context)
            return
        if message.photo and draft is not None:
            await delete_post_step_messages(message.chat_id, context)
            draft["photos"] = [message.photo[-1].file_id]
            await show_generating(message.chat_id, context)
            await post_image_review_step(message.chat_id, context)
            return
        await message.reply_text(
            "📷 Please send the new photo, or tap Cancel.",
            reply_markup=post_step_keyboard([["Cancel"]]),
        )
        return

    if mode == "post_image_review":
        if menu == "Replace image":
            await post_replace_image_step(message.chat_id, context)
            return
        if menu.startswith(LOCK):
            await send_premium_prompt(
                message.chat_id, context,
                f"{LOCK} Free accounts can add only {FREE_POST_IMAGES} image per post.\n\n"
                "\U0001F48E Buy premium to add up to 5 images.",
            )
            return
        if menu == "Add more image":
            if draft is None:
                return
            if len(draft.get("photos", [])) >= MAX_POST_IMAGES:
                await message.reply_text(f"⚠️ You can add up to {MAX_POST_IMAGES} images.")
                return
            await send_post_step(
                message.chat_id, context,
                "📷 Send another photo, or tap Cancel.",
                [["Cancel"]], mode="post_add_image",
            )
            return
        if menu == "Next":
            await post_go_to_button_name(message.chat_id, context)
            return
        return

    if mode == "post_add_image":
        if menu == "Cancel":
            await post_image_review_step(message.chat_id, context)
            return
        if message.photo and draft is not None:
            if len(draft.get("photos", [])) >= MAX_POST_IMAGES:
                await message.reply_text(f"⚠️ You can add up to {MAX_POST_IMAGES} images.")
                await post_image_review_step(message.chat_id, context)
                return
            await delete_post_step_messages(message.chat_id, context)
            draft.setdefault("photos", []).append(message.photo[-1].file_id)
            await show_generating(message.chat_id, context)
            await post_image_review_step(message.chat_id, context)
            return
        await message.reply_text(
            "📷 Please send a photo, or tap Cancel.",
            reply_markup=post_step_keyboard([["Cancel"]]),
        )
        return

    if mode == "post_button_name":
        if menu == "Skip":
            await post_go_to_watermark(message.chat_id, context)
            return
        name = text.strip()[:64]
        if not name:
            await message.reply_text(
                "Please enter a button name, or tap Skip.",
                reply_markup=post_step_keyboard([["Skip"]]),
            )
            return
        context.user_data["pending_button_name"] = name
        color_labels, color_map = post_color_options(is_premium(update.effective_user.id))
        await send_post_step(
            message.chat_id, context,
            "\U0001F3A8 Choose the button colour.",
            [color_labels],
            mode="post_button_color",
            color_map=color_map,
        )
        return

    if mode == "post_button_color":
        if menu.startswith(LOCK):
            await send_premium_prompt(
                message.chat_id, context,
                f"{LOCK} Coloured buttons are a premium feature.\n\n"
                "\U0001F48E Buy premium to use Red / Blue / Green buttons.",
            )
            return
        colors = {"Normal": "normal", "Red": "red", "Blue": "blue", "Green": "green"}
        if menu in colors:
            await send_post_step(
                message.chat_id, context,
                "🔗 Send the button URL. It must start with https:// or http://.",
                [["Cancel"]], mode="post_button_url",
            )
            return
        await message.reply_text("Please choose a colour: Normal, Red, Blue or Green.")
        return

    if mode == "post_button_url":
        if menu == "Cancel":
            await post_go_to_button_name(message.chat_id, context)
            return
        url = text.strip()
        if not re.match(r"^https?://\S+$", url, re.I):
            await message.reply_text(
                "❌ Please send a valid URL, such as https://example.com.",
                reply_markup=post_step_keyboard([["Cancel"]]),
            )
            return
        if draft is None:
            context.user_data["mode"] = "make_post"
            await message.reply_text("Draft not found. Please start again from Make a post.")
            return
        if len(draft.setdefault("buttons", [])) >= MAX_POST_BUTTONS:
            await message.reply_text(f"You can add up to {MAX_POST_BUTTONS} buttons.")
            await post_go_to_buttons(message.chat_id, context)
            return
        draft["buttons"].append({
            "name": context.user_data.pop("pending_button_name", "Button"),
            "url": url,
            "color": context.user_data.pop("pending_button_color", "normal"),
        })
        await show_generating(message.chat_id, context)
        await post_go_to_buttons(message.chat_id, context)
        return

    if mode == "post_buttons":
        if menu.startswith(LOCK):
            await send_premium_prompt(
                message.chat_id, context,
                f"{LOCK} Free accounts can add only {FREE_POST_BUTTONS} button per post.\n\n"
                "\U0001F48E Buy premium to add unlimited buttons.",
            )
            return
        if menu == "Add more button":
            await post_add_button_step(message.chat_id, context)
            return
        if menu == "Edit button":
            await post_edit_select_step(message.chat_id, context)
            return
        if menu == "Button setting":
            await send_post_step(
                message.chat_id, context,
                "⚙️ How many buttons should appear per line? Send a number (1-8).",
                [["Cancel"]], mode="post_button_setting",
            )
            return
        if menu == "Next":
            await post_go_to_watermark(message.chat_id, context)
            return
        return

    if mode == "post_button_setting":
        if menu == "Cancel":
            await post_go_to_buttons(message.chat_id, context)
            return
        if text.strip().isdigit():
            per_line = max(1, min(8, int(text.strip())))
            if draft is not None:
                draft["buttons_per_line"] = per_line
            await show_generating(message.chat_id, context)
            await post_go_to_buttons(message.chat_id, context)
            return
        await message.reply_text(
            "Please send a number between 1 and 8, or tap Cancel.",
            reply_markup=post_step_keyboard([["Cancel"]]),
        )
        return

    if mode == "post_edit_select":
        if menu == "Cancel":
            await post_go_to_buttons(message.chat_id, context)
            return
        buttons = (draft or {}).get("buttons", [])
        index = next((i for i, b in enumerate(buttons) if b.get("name") == menu), None)
        if index is None:
            await message.reply_text(
                "Please choose one of the buttons below.",
                reply_markup=post_step_keyboard([[b.get("name", "Button")] for b in buttons] + [["Cancel"]]),
            )
            return
        context.user_data["edit_button_index"] = index
        await send_post_step(
            message.chat_id, context,
            "✏️ What do you want to change?",
            [["Name", "Color", "URL"], ["Cancel"]], mode="post_edit_field",
        )
        return

    if mode == "post_edit_field":
        if menu == "Cancel":
            await post_go_to_buttons(message.chat_id, context)
            return
        if menu == "Name":
            await send_post_step(message.chat_id, context, "Send the new button name.", [["Cancel"]], mode="post_edit_name")
            return
        if menu == "Color":
            color_labels, color_map = post_color_options(is_premium(update.effective_user.id))
            await send_post_step(
                message.chat_id, context,
                "Choose the new colour.",
                [color_labels],
                mode="post_edit_color",
                color_map=color_map,
            )
            return
        if menu == "URL":
            await send_post_step(message.chat_id, context, "Send the new URL.", [["Cancel"]], mode="post_edit_url")
            return
        return

    if mode == "post_edit_name":
        if menu == "Cancel":
            await post_go_to_buttons(message.chat_id, context)
            return
        index = context.user_data.get("edit_button_index")
        if draft and index is not None and 0 <= index < len(draft.get("buttons", [])):
            draft["buttons"][index]["name"] = text.strip()[:64] or draft["buttons"][index]["name"]
        await show_generating(message.chat_id, context)
        await post_go_to_buttons(message.chat_id, context)
        return

    if mode == "post_edit_color":
        if menu.startswith(LOCK):
            await send_premium_prompt(
                message.chat_id, context,
                f"{LOCK} Coloured buttons are a premium feature.\n\n"
                "\U0001F48E Buy premium to use Red / Blue / Green buttons.",
            )
            return
        colors = {"Normal": "normal", "Red": "red", "Blue": "blue", "Green": "green"}
        if menu == "Cancel":
            await post_go_to_buttons(message.chat_id, context)
            return
        index = context.user_data.get("edit_button_index")
        if menu in colors and draft and index is not None and 0 <= index < len(draft.get("buttons", [])):
            draft["buttons"][index]["color"] = colors[menu]
        await show_generating(message.chat_id, context)
        await post_go_to_buttons(message.chat_id, context)
        return

    if mode == "post_edit_url":
        if menu == "Cancel":
            await post_go_to_buttons(message.chat_id, context)
            return
        url = text.strip()
        if not re.match(r"^https?://\S+$", url, re.I):
            await message.reply_text(
                "❌ Please send a valid URL.",
                reply_markup=post_step_keyboard([["Cancel"]]),
            )
            return
        index = context.user_data.get("edit_button_index")
        if draft and index is not None and 0 <= index < len(draft.get("buttons", [])):
            draft["buttons"][index]["url"] = url
        await show_generating(message.chat_id, context)
        await post_go_to_buttons(message.chat_id, context)
        return

    if mode == "post_watermark_choice":
        if menu == "Continue with watermark":
            await show_generating(message.chat_id, context)
            await post_publish(message.chat_id, context)
            return
        if menu == "Continue without watermark":
            uid = update.effective_user.id
            if user_balance(uid) < 1:
                await message.reply_text(
                    "❌ You need at least 1 credit to remove the watermark. Please top up your balance.",
                    reply_markup=post_step_keyboard([["Continue with watermark"], ["Continue without watermark"]]),
                )
                return
            add_balance(uid, -1)
            if draft is not None:
                remove_watermark(draft)
            await show_generating(message.chat_id, context)
            await post_publish(message.chat_id, context)
            return
        return

    if mode == "post_ready":
        if menu == "Post channel":
            await post_go_to_channels(message.chat_id, context)
            return
        if menu == "Forward":
            if draft is not None:
                await send_post_preview_raw(message.chat_id, context)
            await message.reply_text("📤 Use Telegram's Forward option to share the message above.")
            return
        if menu == "Back to main":
            await show_welcome(message.chat_id, context)
            return
        return

    if mode == "post_channel_list":
        if menu == "Cancel":
            await post_go_to_ready(message.chat_id, context)
            return
        if menu.startswith(LOCK):
            await send_premium_prompt(
                message.chat_id, context,
                f"{LOCK} Channel publishing is a premium feature.\n\n"
                "\U0001F48E Buy premium to add channels and publish posts there.",
            )
            return
        if menu == "Add channel":
            await send_post_step(
                message.chat_id, context,
                "➕ Send the channel username, link and ID.\n"
                "Example: @mychannel https://t.me/mychannel -1001234567890",
                [["Cancel"]], mode="post_channel_add",
            )
            return
        if menu == "Remove channel":
            await send_post_step(
                message.chat_id, context,
                "🗑️ Send the channel username or ID to remove.",
                [["Cancel"]], mode="post_channel_remove",
            )
            return
        uid = update.effective_user.id
        channels = get_user_channels(uid)
        chosen = next(
            (c for c in channels
             if (c.get("title") or c.get("username") or c.get("chat_id")) == menu),
            None,
        )
        if chosen:
            await post_publish_to_channel(message.chat_id, context, chosen)
            return
        return

    if mode == "post_channel_add":
        if menu == "Cancel":
            await post_go_to_channels(message.chat_id, context)
            return
        parsed = parse_channel_input(text)
        if not parsed:
            await message.reply_text(
                "❌ Could not read the channel details. Send username, link and ID.",
                reply_markup=post_step_keyboard([["Cancel"]]),
            )
            return
        context.user_data["pending_channel"] = parsed
        await check_and_add_channel(message.chat_id, context, update.effective_user.id, parsed)
        return

    if mode == "post_channel_check":
        if menu == "Cancel":
            context.user_data.pop("pending_channel", None)
            await post_go_to_channels(message.chat_id, context)
            return
        if menu == "Check again":
            parsed = context.user_data.get("pending_channel")
            if parsed:
                await check_and_add_channel(message.chat_id, context, update.effective_user.id, parsed)
            return
        if menu == "Add me to your channel":
            await send_add_bot_prompt(message.chat_id, context)
            return
        return

    if mode == "post_channel_remove":
        if menu == "Cancel":
            await post_go_to_channels(message.chat_id, context)
            return
        removed = remove_user_channel(update.effective_user.id, text.strip())
        if removed:
            await message.reply_text("✅ Channel removed.")
        else:
            await message.reply_text("❌ Channel not found.")
        await post_go_to_channels(message.chat_id, context)
        return

    # Redeem codes.
    if mode == "redeem_create_users":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ This option is available to admins only."); return
        if not text.strip().isdigit() or int(text.strip()) < 1:
            await message.reply_text(
                "Please send how many users can use the code (a number).",
                reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
            )
            return
        context.user_data["redeem_create_users"] = int(text.strip())
        context.user_data["mode"] = "redeem_create_credits"
        await message.reply_text(
            "How many credits will each user get?",
            reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
        )
        return
    if mode == "redeem_create_credits":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ This option is available to admins only."); return
        if not text.strip().isdigit() or int(text.strip()) < 1:
            await message.reply_text(
                "Please send how many credits each user gets (a number).",
                reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
            )
            return
        users = int(context.user_data.get("redeem_create_users", 1))
        credits = int(text.strip())
        code = create_redeem_code(credits, users, update.effective_user.id)
        context.user_data["mode"] = None
        await message.reply_text(
            "✅ Redeem code created.\n\n"
            f"Code: {code}\n"
            f"Users: {users}\n"
            f"Credits each: {credits}",
            reply_markup=main_menu_for(update, context),
        )
        return
    if mode == "redeem_use":
        code = text.strip().upper()
        uid = update.effective_user.id
        with db() as conn:
            row = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
            used = conn.execute("SELECT 1 FROM redeem_log WHERE user_id=? AND code=?", (uid, code)).fetchone()
        if not row:
            await message.reply_text(
                "❌ Invalid code.",
                reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
            )
            return
        if used:
            await message.reply_text(
                "❌ You have already redeemed this code.",
                reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
            )
            return
        if int(row["uses"]) >= int(row["max_uses"]):
            await message.reply_text(
                "❌ This code has reached its usage limit.",
                reply_markup=green_reply_markup([[KeyboardButton("Cancel")]]),
            )
            return
        with db() as conn:
            conn.execute("INSERT INTO redeem_log(user_id, code) VALUES(?, ?)", (uid, code))
            conn.execute("UPDATE redeem_codes SET uses = uses + 1 WHERE code=?", (code,))
        balance = add_balance(uid, int(row["reward"]))
        context.user_data["mode"] = None
        await message.reply_text(
            f"✅ Code redeemed: +{int(row['reward'])} coins\n💰 Balance: {balance} coins",
            reply_markup=main_menu_for(update, context),
        )
        return

    # Make-post token handling works even after paging/favorites navigation.
    if mode == "make_post" and text and await begin_post_from_text(message, text, context):
        return

    if mode == "category_add":
        name = text.strip()[:64]
        if name:
            with db() as conn:
                existing = conn.execute("SELECT category_id FROM categories WHERE name=?", (name,)).fetchone()
                if existing:
                    row = existing
                else:
                    next_id = conn.execute("SELECT COALESCE(MAX(category_id),0)+1 FROM categories").fetchone()[0]
                    next_order = conn.execute("SELECT COALESCE(MAX(display_order),0)+1 FROM categories").fetchone()[0]
                    conn.execute("INSERT INTO categories(name, category_id, display_order) VALUES(?,?,?)", (name, next_id, next_order))
                    row = conn.execute("SELECT category_id FROM categories WHERE name=?", (name,)).fetchone()
            await message.reply_text(f"✅ Category created: {name}\n🆔 Category Serial ID: {row['category_id']}")
        context.user_data.pop("mode", None); return

    if mode in ("category_manage_add", "category_manage_remove"):
        category = context.user_data.get("current_category")
        if not category: await message.reply_text("No category selected."); return
        all_rows = get_emojis()
        ids = serial_to_ids(text, all_rows)
        if mode == "category_manage_add":
            present = {r['emoji_id'] for r in get_category_emojis(category)}
            with db() as conn:
                for eid in ids:
                    if any(r['emoji_id'] == eid for r in all_rows) and eid not in present:
                        next_cat_order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM emoji_categories WHERE category=?", (category,)).fetchone()[0]
                        conn.execute("INSERT OR IGNORE INTO emoji_categories(emoji_id, category, sort_order) VALUES(?,?,?)", (eid, category, next_cat_order))
            await message.reply_text(f"✅ Added {sum(1 for eid in ids if eid not in present and any(r['emoji_id']==eid for r in all_rows))} emoji(s) to the category.")
        else:
            with db() as conn:
                for eid in ids: conn.execute("DELETE FROM emoji_categories WHERE emoji_id=? AND category=?", (eid, category))
            await message.reply_text(f"✅ Removed {len(ids)} ID(s) from the category.")
        await show_category_page(update, context, category, 0, admin=True); return

    if mode == "remove_emoji":
        if not is_admin(update.effective_user.id):
            await message.reply_text("⛔ This option is available to admins only."); return
        ids = serial_to_ids(text, get_emojis())
        if not ids:
            await message.reply_text("❌ No valid Emoji IDs found.")
            return
        removed = remove_emoji_ids(ids)
        await message.reply_text(f"🗑️ Removed {removed} ID(s) from the database.")
        return

    if mode in ("favorite_add", "favorite_remove"):
        uid = update.effective_user.id
        rows_for_serials = get_emojis(favorites=True, user_id=uid) if mode == "favorite_remove" else get_emojis()
        ids = serial_to_ids(text, rows_for_serials)
        if not ids:
            await message.reply_text(
                "❌ No valid Emoji IDs found. Please send an emoji ID.",
                reply_markup=favorite_cancel_keyboard(),
            )
            return
        if mode == "favorite_add":
            if not is_premium(uid):
                current = len(get_emojis(favorites=True, user_id=uid))
                remaining = max(0, FAVORITE_LIMIT - current)
                if remaining <= 0:
                    await send_premium_prompt(
                        message.chat_id, context,
                        f"{LOCK} Your free favorite list is full ({FAVORITE_LIMIT} emojis).\n\n"
                        "\U0001F48E Buy premium to add unlimited favorite emojis.",
                    )
                    return
                if len(ids) > remaining:
                    ids = ids[:remaining]
            found = set_favorites(ids, True, user_id=uid)
            missing = len(set(ids) - set(found))
            reply = f"⭐ Added {len(found)} emoji ID(s) to your favorites."
            if missing:
                reply += f"\n⚠️ Not found in the database: {missing}"
        else:
            found = set_favorites(ids, False, user_id=uid)
            reply = f"✅ Removed {len(found)} emoji ID(s) from your favorites."
        await message.reply_text(reply)
        context.user_data["mode"] = "make_post"
        context.user_data.pop("active_category", None)
        await show_emoji_page(update, context, 0, favorites=True)
        await ensure_emoji_keyboard(message, context, favorites=True, force=True)
        return

    entities = extract_entities(message)

    if mode == "add_emoji":
        if pack_name:
            await add_pack(message, pack_name, context)
            return
        added = 0; duplicates = 0; invalid = 0
        candidates = [(eid, alt) for eid, alt in entities] + [(eid, "😀") for eid in parse_ids(text)]
        seen_input = set()
        for emoji_id, alt in candidates:
            if emoji_id in seen_input:
                duplicates += 1; continue
            seen_input.add(emoji_id)
            with db() as conn: existing = conn.execute("SELECT sort_order FROM emojis WHERE emoji_id=?", (str(emoji_id),)).fetchone()
            if existing:
                # Refresh/re-add the existing ID at its exact same global serial position.
                save_emoji(emoji_id, alt, preserve_order=existing['sort_order'])
                duplicates += 1
            elif save_emoji(emoji_id, alt): added += 1
            else: invalid += 1
        if added or duplicates:
            reply = f"✅ Newly added: {added}"
            if duplicates: reply += f"\n⚠️ Already in the database: {duplicates}"
            if invalid: reply += f"\n❌ Invalid IDs: {invalid}"
            await message.reply_text(reply)
        else:
            await message.reply_text("❌ No Premium Emoji IDs or valid IDs found.")
        return

    if mode == "view_emoji":
        if pack_name:
            try:
                result = await user_client(GetStickerSetRequest(
                    stickerset=InputStickerSetShortName(short_name=pack_name), hash=0
                ))
                lines = [f"🔎 {result.set.title}", f"Total emojis: {len(result.documents)}", ""]
                for doc in result.documents[:100]:
                    lines.append(f"{get_custom_emoji_alt(doc)} — {{{doc.id}}}")
                lines.append("\nTo add these to the database, tap ➕ Add emoji and send the pack link.")
                await message.reply_text("\n".join(lines))
            except Exception as exc:
                print("View pack error:", repr(exc))
                await message.reply_text("❌ Could not read the pack.")
            return
        if entities:
            lines = ["🔎 Premium Emoji IDs found:"]
            for emoji_id, alt in entities:
                lines.append(f"{alt} — {emoji_id}")
            await message.reply_text("\n".join(lines))
            return
        ids = parse_ids(text)
        if ids:
            output = []
            with db() as conn:
                for emoji_id in ids:
                    row = conn.execute("SELECT alt FROM emojis WHERE emoji_id=?", (emoji_id,)).fetchone()
                    output.append(f"{row['alt'] if row else '😀'} — {emoji_id}")
            await message.reply_text("🔎 Emoji ID:\n" + "\n".join(output))
            return
        await message.reply_text("No Premium Emoji IDs found. Please send or forward a message containing Premium Emoji.")
        return

    # Default: report IDs from premium-emoji messages, or explain token syntax.
    if entities:
        await message.reply_text("🔎 Custom Emoji ID:\n" + "\n".join(f"{alt} — {emoji_id}" for emoji_id, alt in entities))
    elif pack_name:
        await add_pack(message, pack_name, context)
    else:
        await message.reply_text(
            "Please choose an option from the menu below.\n\n"
            "In Make a post, use a format like `Hello {5942533496823093848}`.\n"
            "/help — Help",
            reply_markup=main_menu_for(update, context),
        )


async def cng_emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /cng_emoji <button_id> <emoji_id> — change a menu button's icon."""
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ This command is available to admins only.")
        return
    args = context.args
    if len(args) != 2 or not args[0].isdigit():
        await update.effective_message.reply_text("Usage: /cng_emoji <button_id> <emoji_id>")
        return
    button = get_menu_button(int(args[0]))
    if not button:
        await update.effective_message.reply_text("❌ Button ID not found. Use /id to see button IDs.")
        return
    emoji_id = args[1].strip().strip("{}()[]")
    if not re.fullmatch(r"\d{5,30}", emoji_id):
        await update.effective_message.reply_text("❌ Invalid emoji ID.")
        return
    with db() as conn:
        conn.execute("UPDATE menu_buttons SET emoji_id=? WHERE btn_id=?", (emoji_id, button["btn_id"]))
    await update.effective_message.reply_text(
        f"✅ Button {button['btn_id']} ({button['text']}) now uses emoji {emoji_id}.",
        reply_markup=main_menu_for(update, context),
    )


async def cng_color_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /cng_calel <button_id> <color> — change a menu button's colour."""
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ This command is available to admins only.")
        return
    args = context.args
    if len(args) != 2 or not args[0].isdigit():
        await update.effective_message.reply_text("Usage: /cng_calel <button_id> <red|blue|green|normal>")
        return
    button = get_menu_button(int(args[0]))
    if not button:
        await update.effective_message.reply_text("❌ Button ID not found. Use /id to see button IDs.")
        return
    color = args[1].strip().lower()
    if color not in {"red", "blue", "green", "normal"}:
        await update.effective_message.reply_text("❌ Colour must be red, blue, green or normal.")
        return
    with db() as conn:
        conn.execute("UPDATE menu_buttons SET color=? WHERE btn_id=?", (color, button["btn_id"]))
    await update.effective_message.reply_text(
        f"✅ Button {button['btn_id']} ({button['text']}) is now {color}.",
        reply_markup=main_menu_for(update, context),
    )


async def cng_order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /cng_order <button_id> <order> — move a menu button."""
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ This command is available to admins only.")
        return
    args = context.args
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.effective_message.reply_text("Usage: /cng_order <button_id> <order>")
        return
    button = get_menu_button(int(args[0]))
    if not button:
        await update.effective_message.reply_text("❌ Button ID not found. Use /id to see button IDs.")
        return
    order = max(1, int(args[1]))
    if not reorder_menu_button(button["btn_id"], order):
        await update.effective_message.reply_text("❌ Could not move that button.")
        return
    await update.effective_message.reply_text(
        f"✅ Button {button['btn_id']} ({button['text']}) moved to position {order}.",
        reply_markup=main_menu_for(update, context),
    )


async def send_edit_button_info(chat_id, context, btn_id):
    """Show a button's name + ID with the edit action keyboard."""
    btn = get_menu_button(btn_id)
    if not btn:
        await context.bot.send_message(chat_id=chat_id, text="❌ Button not found.")
        return
    text = (
        "✏️ Edit button\n\n"
        f"Name: {btn['text']}\n"
        f"Button ID: {btn['btn_id']}\n"
        f"Colour: {btn['color']}\n"
        f"Emoji ID: {btn['emoji_id'] or '—'}"
    )
    markup = {"inline_keyboard": [
        [{"text": "Change name", "callback_data": f"eb:name:{btn['btn_id']}"},
         {"text": "Change emoji", "callback_data": f"eb:emoji:{btn['btn_id']}"}],
        [{"text": "Change color", "callback_data": f"eb:color:{btn['btn_id']}"},
         {"text": "Change position", "callback_data": f"eb:pos:{btn['btn_id']}"}],
        [{"text": "Done", "callback_data": f"eb:done:{btn['btn_id']}"}],
    ]}
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


async def send_edit_position(chat_id, context, btn_id):
    markup = {"inline_keyboard": [
        [{"text": "⬆️ Up", "callback_data": f"eb:move:{btn_id}:up"}],
        [{"text": "⬅️ Left", "callback_data": f"eb:move:{btn_id}:left"},
         {"text": "➡️ Right", "callback_data": f"eb:move:{btn_id}:right"}],
        [{"text": "⬇️ Down", "callback_data": f"eb:move:{btn_id}:down"}],
        [{"text": "🔙 Back", "callback_data": f"eb:pos:{btn_id}"}],
    ]}
    await context.bot.send_message(
        chat_id=chat_id, text="↕️ Use the buttons below to move this button.",
        reply_markup=markup,
    )


async def send_edit_colors(chat_id, context, btn_id):
    markup = {"inline_keyboard": [
        [{"text": "🔴 Red", "callback_data": f"eb:setcolor:{btn_id}:red"},
         {"text": "🔵 Blue", "callback_data": f"eb:setcolor:{btn_id}:blue"}],
        [{"text": "🟢 Green", "callback_data": f"eb:setcolor:{btn_id}:green"},
         {"text": "⚪ Normal", "callback_data": f"eb:setcolor:{btn_id}:normal"}],
    ]}
    await context.bot.send_message(
        chat_id=chat_id, text="🎨 Choose the new colour.", reply_markup=markup,
    )


async def edit_button_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /edit_button — pick a button on the current menu and edit it."""
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ This command is available to admins only.")
        return
    sync_category_buttons()
    menu_context = current_menu_context(context)
    context.user_data["edit_menu_context"] = menu_context
    context.user_data["edit_return_mode"] = context.user_data.get("mode")
    context.user_data["mode"] = "edit_button_select"
    await update.effective_message.reply_text(
        "✏️ Select any button from the menu below to edit.",
        reply_markup=menu_markup_for_context(context, menu_context),
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the ID of whichever button the user taps next, keeping the current menu."""
    track_user(update)
    if not await ensure_joined(update, context):
        return
    menu_context = current_menu_context(context)
    context.user_data["id_return_mode"] = context.user_data.get("mode")
    context.user_data["id_menu_context"] = menu_context
    context.user_data["mode"] = "id_mode"
    await update.effective_message.reply_text(
        "🆔 Please click any button.\nTap Cancel to exit.",
        reply_markup=menu_markup_for_context(context, menu_context),
    )


async def p_page_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In Make a post, /pN jumps the emoji list to page N."""
    message = update.effective_message
    track_user(update)
    if not await ensure_joined(update, context):
        return
    match = re.fullmatch(r"/p(\d+)", (message.text or "").strip())
    if not match:
        return
    page = max(1, int(match.group(1))) - 1
    favorites = bool(context.user_data.get("showing_favorites"))
    context.user_data["mode"] = "make_post"
    await show_emoji_page(update, context, page, favorites=favorites)
    await ensure_emoji_keyboard(message, context, favorites=favorites)


async def cng_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ This command is available to admins only."); return
    args = context.args
    if len(args) == 2 and re.fullmatch(r"cat\d+", args[0], re.I) and re.fullmatch(r"cat\d+", args[1], re.I):
        first, second = int(args[0][3:]), int(args[1][3:])
        with db() as conn:
            cats = conn.execute("SELECT name, category_id, display_order FROM categories ORDER BY display_order, category_id").fetchall()
            if not (1 <= first <= len(cats) and 1 <= second <= len(cats)):
                await update.effective_message.reply_text("❌ No category found at that serial number."); return
            a, b = cats[first-1], cats[second-1]
            if first != second:
                conn.execute("UPDATE categories SET display_order=-1 WHERE name=?", (a["name"],))
                conn.execute("UPDATE categories SET display_order=? WHERE name=?", (a["display_order"], b["name"]))
                conn.execute("UPDATE categories SET display_order=? WHERE name=?", (b["display_order"], a["name"]))
        await update.effective_message.reply_text(f"✅ Swapped the positions of Category {first} and Category {second} in Make a post.")
        return
    if len(args) == 2:
        try: serial = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("Usage: /cng <serial_id> <emoji_id>"); return
        new_id = args[1].strip().strip('{}()[]')
        rows = get_emojis()
        if not (1 <= serial <= len(rows)) or not any(r['emoji_id'] == new_id for r in rows):
            await update.effective_message.reply_text("❌ Serial number or Emoji ID not found in the database."); return
        target = rows[serial-1]['emoji_id']
        with db() as conn:
            # Swap positions so the requested emoji occupies the chosen serial and the old one takes its former position.
            other = conn.execute("SELECT sort_order FROM emojis WHERE emoji_id=?", (new_id,)).fetchone()
            here = conn.execute("SELECT sort_order FROM emojis WHERE emoji_id=?", (target,)).fetchone()
            if other and here:
                conn.execute("UPDATE emojis SET sort_order=-1 WHERE emoji_id=?", (target,))
                conn.execute("UPDATE emojis SET sort_order=? WHERE emoji_id=?", (here['sort_order'], new_id))
                conn.execute("UPDATE emojis SET sort_order=? WHERE emoji_id=?", (other['sort_order'], target))
        await update.effective_message.reply_text(f"✅ Serial {serial} now displays {new_id}. The previous emoji has moved to the selected emoji’s former position.")
        return
    if len(args) == 3:
        try: cat_id, serial = int(args[0]), int(args[1])
        except ValueError:
            await update.effective_message.reply_text("Usage: /cng <category_id> <serial_id> <emoji_id>"); return
        new_id = args[2].strip().strip('{}()[]')
        with db() as conn:
            cat = conn.execute("SELECT name FROM categories WHERE category_id=?", (cat_id,)).fetchone()
        if not cat:
            await update.effective_message.reply_text("❌ Category ID not found."); return
        category = cat['name']; rows = get_category_emojis(category)
        if not (1 <= serial <= len(rows)):
            await update.effective_message.reply_text("❌ That serial number does not exist in this category."); return
        if not any(r['emoji_id'] == new_id for r in rows):
            await update.effective_message.reply_text("❌ This Emoji ID is not in that category. Add it to the category first."); return
        target = rows[serial-1]['emoji_id']
        with db() as conn:
            other = conn.execute("SELECT sort_order FROM emoji_categories WHERE category=? AND emoji_id=?", (category, new_id)).fetchone()
            here = conn.execute("SELECT sort_order FROM emoji_categories WHERE category=? AND emoji_id=?", (category, target)).fetchone()
            if other and here:
                conn.execute("UPDATE emoji_categories SET sort_order=-1 WHERE category=? AND emoji_id=?", (category, target))
                conn.execute("UPDATE emoji_categories SET sort_order=? WHERE category=? AND emoji_id=?", (here['sort_order'], category, new_id))
                conn.execute("UPDATE emoji_categories SET sort_order=? WHERE category=? AND emoji_id=?", (other['sort_order'], category, target))
        await update.effective_message.reply_text(f"✅ Updated serial {serial} in Category {cat_id} ({category}).")
        return
    await update.effective_message.reply_text("Usage:\n/cng <serial_id> <emoji_id>\n/cng <category_id> <serial_id> <emoji_id>\n/cng cat1 cat5 — Swap category positions in Make a post")


# ---------------------------------------------------------------------------
# Embedded web dashboard (Flask). Running bot.py starts both the bot and this
# dashboard, and prints the admin password in the terminal on startup.
# ---------------------------------------------------------------------------

def _load_web_password():
    env_password = os.getenv("WEB_PASSWORD", "").strip()
    if env_password:
        return env_password
    path = os.getenv("WEB_PASSWORD_FILE", "web_admin_password.txt")
    try:
        if os.path.exists(path):
            saved = open(path, "r", encoding="utf-8").read().strip()
            if saved:
                return saved
    except OSError:
        pass
    password = secrets.token_urlsafe(9)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(password)
    except OSError:
        pass
    return password


WEB_PASSWORD = _load_web_password()
web_app = Flask(__name__)
web_app.secret_key = os.getenv("WEB_SECRET_KEY") or secrets.token_hex(24)

WEB_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0b1020;color:#e8ecf8;min-height:100vh}
a{color:#8b9dff;text-decoration:none}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;flex-wrap:wrap;gap:12px}
h1{font-size:24px;font-weight:700;letter-spacing:.3px}
h1 span{background:linear-gradient(90deg,#7c5cff,#3ad0ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.badge{font-size:12px;color:#9aa7c7;border:1px solid #26304d;padding:6px 12px;border-radius:999px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:28px}
.card{background:linear-gradient(160deg,#141b33,#10162a);border:1px solid #232c49;border-radius:16px;padding:20px}
.card .label{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#8b98bb}
.card .value{font-size:34px;font-weight:800;margin-top:8px;background:linear-gradient(90deg,#7c5cff,#3ad0ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.panel{background:#10162a;border:1px solid #232c49;border-radius:16px;padding:22px;margin-bottom:22px}
.panel h2{font-size:16px;margin-bottom:16px;color:#cfd8f5}
form.inline{display:flex;gap:10px;flex-wrap:wrap}
input[type=text],input[type=password]{flex:1;min-width:200px;background:#0b1020;border:1px solid #2b3556;color:#e8ecf8;border-radius:10px;padding:11px 14px;font-size:14px;outline:none}
select{background:#0b1020;border:1px solid #2b3556;color:#e8ecf8;border-radius:10px;padding:11px 14px;font-size:14px;outline:none}
input:focus{border-color:#5b6cff}
button{background:linear-gradient(90deg,#6a4dff,#2fb8ff);border:none;color:#fff;font-weight:600;padding:11px 20px;border-radius:10px;cursor:pointer;font-size:14px}
button:hover{filter:brightness(1.1)}
button.danger{background:#3a1b2b;color:#ff8aa8;border:1px solid #5a2438}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #1d2540}
th{color:#8b98bb;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.6px}
tr:last-child td{border-bottom:none}
.muted{color:#7d8ab0;font-size:13px}
.flash{padding:12px 16px;border-radius:10px;margin-bottom:16px;font-size:14px}
.flash.ok{background:#122b1f;border:1px solid #1f5c3c;color:#7ee2a8}
.flash.err{background:#2b141b;border:1px solid #5c1f30;color:#ff9db4}
.login{max-width:380px;margin:14vh auto;background:#10162a;border:1px solid #232c49;border-radius:18px;padding:32px}
.login h1{font-size:22px;margin-bottom:6px}
.login p{color:#8b98bb;font-size:13px;margin-bottom:22px}
.login input{width:100%;margin-bottom:14px}
.login button{width:100%}
"""

LOGIN_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard Login</title><style>{{ css|safe }}</style></head><body>
<div class="login"><h1><span>Premium Emoji Bot</span></h1>
<p>Enter the admin password printed in the bot terminal.</p>
{% if error %}<div class="flash err">{{ error }}</div>{% endif %}
<form method="post" action="{{ url_for('web_login') }}">
<input type="password" name="password" placeholder="Admin password" autofocus>
<button type="submit">Sign in</button></form></div></body></html>"""

DASHBOARD_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Premium Emoji Bot — Dashboard</title><style>{{ css|safe }}</style></head><body>
<div class="wrap">
<header><h1><span>Premium Emoji Bot</span> Dashboard</h1>
<div><span class="badge">Admin</span> <a class="badge" href="{{ url_for('web_logout') }}">Log out</a></div></header>
{% if message %}<div class="flash {{ 'ok' if ok else 'err' }}">{{ message }}</div>{% endif %}
<div class="grid">
<div class="card"><div class="label">Total Users</div><div class="value">{{ stats.users }}</div></div>
<div class="card"><div class="label">Categories</div><div class="value">{{ stats.categories }}</div></div>
<div class="card"><div class="label">Emojis</div><div class="value">{{ stats.emojis }}</div></div>
<div class="card"><div class="label">Pending Orders</div><div class="value">{{ stats.pending }}</div></div>
</div>

<div class="panel"><h2>Orders</h2>
<table><thead><tr><th>#</th><th>User</th><th>Kind</th><th>Package</th><th>Amount</th><th>Price</th><th>Method</th><th>Trx</th><th>Status</th><th>Actions</th></tr></thead><tbody>
{% for o in orders %}<tr>
<td>{{ o.id }}</td>
<td class="muted">{{ o.user_id }}<br>{{ '@' + o.username if o.username else '' }}</td>
<td>{{ o.kind }}</td><td>{{ o.package_label }}</td><td>{{ o.amount }}</td><td>{{ o.price }}</td>
<td class="muted">{{ o.method }}{% if o.screenshot_file_id %}<br>📸 screenshot{% endif %}</td>
<td class="muted">{{ o.trx or '—' }}</td>
<td><span class="badge">{{ o.status }}</span></td>
<td>
{% if o.status == 'pending' %}
<form method="post" action="{{ url_for('web_approve_order', order_id=o.id) }}" style="display:inline"><button type="submit">Approve</button></form>
<form method="post" action="{{ url_for('web_reject_order', order_id=o.id) }}" style="display:inline"><button class="danger" type="submit">Reject</button></form>
{% else %}—{% endif %}
</td></tr>
{% else %}<tr><td colspan="10" class="muted">No orders yet.</td></tr>{% endfor %}
</tbody></table></div>

<div class="panel"><h2>Shop packages</h2>
<form class="inline" method="post" action="{{ url_for('web_add_package') }}">
<select name="kind"><option value="credit">Credit</option><option value="premium">Premium</option><option value="ff">FF like</option></select>
<input type="text" name="label" placeholder="Label (e.g. 25 credit)">
<input type="text" name="amount" placeholder="Amount (credits/days/likes)">
<input type="text" name="price" placeholder="Price (taka)">
<button type="submit">Add package</button></form>
<table style="margin-top:16px"><thead><tr><th>Kind</th><th>Label</th><th>Amount</th><th>Price</th><th></th></tr></thead><tbody>
{% for p in packages %}<tr><td>{{ p.kind }}</td><td>{{ p.label }}</td><td>{{ p.amount }}</td><td>{{ p.price }}</td>
<td><form method="post" action="{{ url_for('web_delete_package', package_id=p.id) }}"><button class="danger" type="submit">Remove</button></form></td></tr>
{% else %}<tr><td colspan="5" class="muted">No packages.</td></tr>{% endfor %}
</tbody></table></div>

<div class="panel"><h2>Payment & contact settings</h2>
<form class="inline" method="post" action="{{ url_for('web_save_settings') }}">
<input type="text" name="bikash_number" value="{{ settings.bikash_number }}" placeholder="Bikash number">
<input type="text" name="nagad_number" value="{{ settings.nagad_number }}" placeholder="Nagad number">
<input type="text" name="whatsapp_link" value="{{ settings.whatsapp_link }}" placeholder="WhatsApp link">
<input type="text" name="telegram_link" value="{{ settings.telegram_link }}" placeholder="Telegram link">
<input type="text" name="redeem_fee" value="{{ settings.redeem_fee }}" placeholder="Redeem fee">
<input type="text" name="web_base_url" value="{{ settings.web_base_url }}" placeholder="Public URL (https://your-domain.com)">
<button type="submit">Save settings</button></form></div>

<div class="panel"><h2>Add a required channel</h2>
<form class="inline" method="post" action="{{ url_for('web_add_channel') }}">
<input type="text" name="channel" placeholder="@username or -1001234567890">
<button type="submit">Add channel</button></form>
<p class="muted" style="margin-top:12px">The bot must be an administrator in the channel so it can verify membership.</p></div>
<div class="panel"><h2>Required channels</h2>
<table><thead><tr><th>Title</th><th>Target</th><th>Link</th><th></th></tr></thead><tbody>
{% for c in channels %}<tr><td>{{ c.title or '—' }}</td><td class="muted">{{ c.username or c.chat_id or '—' }}</td>
<td>{% if c.link %}<a href="{{ c.link }}" target="_blank">open</a>{% else %}—{% endif %}</td>
<td><form method="post" action="{{ url_for('web_delete_channel', channel_id=c.id) }}">
<button class="danger" type="submit">Remove</button></form></td></tr>
{% else %}<tr><td colspan="4" class="muted">No channels yet.</td></tr>{% endfor %}
</tbody></table></div>
<div class="panel"><h2>Recent users</h2>
<table><thead><tr><th>User ID</th><th>Username</th><th>Name</th><th>Balance</th><th>Joined</th></tr></thead><tbody>
{% for u in users %}<tr><td>{{ u.user_id }}</td><td class="muted">{{ '@' + u.username if u.username else '—' }}</td>
<td>{{ u.first_name or '—' }}</td><td>{{ u.balance }}</td><td class="muted">{{ u.joined_at }}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No users yet.</td></tr>{% endfor %}
</tbody></table></div>
</div></body></html>"""


def web_login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("web_admin"):
            return redirect(url_for("web_login"))
        return view(*args, **kwargs)
    return wrapped


def normalize_channel(raw):
    """Parse a channel reference into (chat_id, username, link)."""
    value = (raw or "").strip()
    if not value:
        return None
    if value.startswith("https://t.me/") or value.startswith("t.me/"):
        value = value.split("t.me/", 1)[1].strip("/")
        if value.startswith("+") or value.startswith("joinchat"):
            return None
    if value.startswith("@"):
        username = value[1:]
        return ("", username, f"https://t.me/{username}")
    if re.fullmatch(r"-?\d{5,}", value):
        return (value, "", "")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", value):
        return ("", value, f"https://t.me/{value}")
    return None


@web_app.route("/login", methods=["GET", "POST"])
def web_login():
    if request.method == "POST":
        if request.form.get("password", "") == WEB_PASSWORD:
            session["web_admin"] = True
            return redirect(url_for("web_dashboard"))
        return render_template_string(LOGIN_TEMPLATE, css=WEB_CSS, error="Incorrect password.")
    return render_template_string(LOGIN_TEMPLATE, css=WEB_CSS, error=None)


@web_app.route("/logout")
def web_logout():
    session.pop("web_admin", None)
    return redirect(url_for("web_login"))


@web_app.route("/")
@web_login_required
def web_dashboard():
    with db() as conn:
        stats = {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "categories": conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
            "emojis": conn.execute("SELECT COUNT(*) FROM emojis").fetchone()[0],
            "channels": conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            "pending": conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0],
        }
        users = [dict(r) for r in conn.execute(
            "SELECT user_id, username, first_name, balance, joined_at FROM users "
            "ORDER BY joined_at DESC LIMIT 25"
        ).fetchall()]
        channels = []
        for row in conn.execute("SELECT * FROM channels ORDER BY id").fetchall():
            item = dict(row)
            item["link"] = channel_link(item)
            channels.append(item)
        orders = [dict(r) for r in conn.execute(
            "SELECT o.*, u.username FROM orders o LEFT JOIN users u ON u.user_id = o.user_id "
            "ORDER BY o.id DESC LIMIT 100"
        ).fetchall()]
        packages = [dict(r) for r in conn.execute(
            "SELECT * FROM shop_packages ORDER BY kind, sort_order, id"
        ).fetchall()]
        settings = {
            key: get_setting(key)
            for key in ("bikash_number", "nagad_number", "whatsapp_link", "telegram_link", "redeem_fee")
        }
    message = request.args.get("message")
    ok = request.args.get("ok") == "1"
    return render_template_string(
        DASHBOARD_TEMPLATE, css=WEB_CSS, stats=stats, users=users,
        channels=channels, orders=orders, packages=packages, settings=settings,
        message=message, ok=ok,
    )


@web_app.route("/orders/<int:order_id>/approve", methods=["POST"])
def web_approve_order(order_id):
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    order = approve_order(order_id)
    if not order:
        return redirect(url_for("web_dashboard", message="Order not found.", ok="0"))
    if order["kind"] == "credit":
        notify_user(
            order["user_id"],
            f"✅ Your order #{order['id']} was approved!\n"
            f"+{order['amount']} credits added to your balance.",
        )
    elif order["kind"] == "premium":
        notify_user_markup(
            order["user_id"],
            f"\u2705 Your order #{order['id']} was approved!\n"
            f"\u2b50 Premium package: {order['amount']} days\n\n"
            "\U0001F4A1 Tap the button below to activate premium on your account.",
            {"inline_keyboard": [[{
                "text": "\U0001F48E Active premium now",
                "callback_data": f"prem:activate:{order['id']}",
                "style": "success",
            }]]},
        )
    else:
        notify_user(
            order["user_id"],
            f"✅ Your order #{order['id']} was approved!\n"
            f"{order['package_label']} will be delivered soon.",
        )
    return redirect(url_for("web_dashboard", message=f"Order #{order_id} approved.", ok="1"))


@web_app.route("/orders/<int:order_id>/reject", methods=["POST"])
def web_reject_order(order_id):
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    order = reject_order(order_id)
    if not order:
        return redirect(url_for("web_dashboard", message="Order not found.", ok="0"))
    notify_user(
        order["user_id"],
        f"❌ Your order #{order['id']} was rejected.\n"
        "Please contact support if you think this is a mistake.",
    )
    return redirect(url_for("web_dashboard", message=f"Order #{order_id} rejected.", ok="1"))


@web_app.route("/packages/add", methods=["POST"])
def web_add_package():
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    kind = request.form.get("kind", "credit").strip()
    label = request.form.get("label", "").strip()
    amount = request.form.get("amount", "0").strip()
    price = request.form.get("price", "0").strip()
    if kind not in {"credit", "premium", "ff"} or not label or not amount.isdigit() or not price.isdigit():
        return redirect(url_for("web_dashboard", message="Enter a valid kind, label, amount and price.", ok="0"))
    with db() as conn:
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM shop_packages WHERE kind=?", (kind,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO shop_packages(kind, label, amount, price, sort_order) VALUES(?,?,?,?,?)",
            (kind, label, int(amount), int(price), next_order),
        )
    return redirect(url_for("web_dashboard", message="Package added.", ok="1"))


@web_app.route("/packages/<int:package_id>/delete", methods=["POST"])
def web_delete_package(package_id):
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    with db() as conn:
        conn.execute("DELETE FROM shop_packages WHERE id=?", (package_id,))
    return redirect(url_for("web_dashboard", message="Package removed.", ok="1"))


@web_app.route("/settings", methods=["POST"])
def web_save_settings():
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    for key in ("bikash_number", "nagad_number", "whatsapp_link", "telegram_link",
                "redeem_fee", "web_base_url", "block_vpn"):
        if key in request.form:
            set_setting(key, request.form.get(key, "").strip())
    return redirect(url_for("web_dashboard", message="Settings saved.", ok="1"))


@web_app.route("/channels/add", methods=["POST"])
def web_add_channel():
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    parsed = normalize_channel(request.form.get("channel", ""))
    if not parsed:
        return redirect(url_for("web_dashboard", message="Enter a public @username or a numeric channel ID. An invite link alone cannot be verified by the bot.", ok="0"))
    chat_id, username, link = parsed
    with db() as conn:
        conn.execute(
            "INSERT INTO channels(chat_id, username, invite_link) VALUES(?, ?, ?)",
            (chat_id, username, link),
        )
    return redirect(url_for("web_dashboard", message="Channel added.", ok="1"))


@web_app.route("/channels/<int:channel_id>/delete", methods=["POST"])
def web_delete_channel(channel_id):
    if not session.get("web_admin"):
        return redirect(url_for("web_login"))
    with db() as conn:
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    return redirect(url_for("web_dashboard", message="Channel removed.", ok="1"))


# ---------------------------------------------------------------------------
# Free premium claim page (one claim per device / IP).
# ---------------------------------------------------------------------------
CLAIM_WEB = {
    "en": {
        "title": "Claim 3 days Premium", "heading": "3 Days Premium",
        "subtitle": "Tap the button below to activate 3 days of Premium on your Telegram account.",
        "button": "Claim now", "already": "Your free premium has already been claimed on this device.",
        "note": "One free premium per device. VPN and cloned apps are not eligible.",
        "unknown": "This Telegram account is not known to the bot yet. Start the bot first.",
        "claimed_acc": "This account has already claimed the free premium.",
        "vpn": "VPN / proxy detected. Free premium cannot be claimed through a VPN.",
        "no_ip": "Could not read your IP address. Please try again from a normal connection.",
        "ip_used": "This device (IP) has already claimed the free premium.",
        "dev_used": "This device has already claimed the free premium.",
        "success": "\U0001F389 Congratulations! 3 days of Premium is now active on your account.",
        "notify": "\U0001F389 Your 3 days Premium is now active!\n\u23F0 Valid until: {until}\n\n"
                  "Enjoy unlimited favorites, images, buttons, colours and channels.",
    },
    "bn": {
        "title": "৩ দিনের প্রিমিয়াম নিন", "heading": "৩ দিনের প্রিমিয়াম",
        "subtitle": "আপনার টেলিগ্রাম অ্যাকাউন্টে ৩ দিনের প্রিমিয়াম চালু করতে নিচের বাটনে ক্লিক করুন।",
        "button": "এখনই নিন", "already": "এই ডিভাইসে ইতিমধ্যে ফ্রি প্রিমিয়াম নেওয়া হয়েছে।",
        "note": "একটি ডিভাইসে একবারই ফ্রি প্রিমিয়াম। VPN ও ক্লোন অ্যাপ প্রযোজ্য নয়।",
        "unknown": "এই টেলিগ্রাম অ্যাকাউন্টটি বট এখনো চেনে না। আগে বটটি স্টার্ট করুন।",
        "claimed_acc": "এই অ্যাকাউন্ট ইতিমধ্যে ফ্রি প্রিমিয়াম নিয়েছে।",
        "vpn": "VPN / প্রক্সি শনাক্ত হয়েছে। VPN দিয়ে ফ্রি প্রিমিয়াম নেওয়া যাবে না।",
        "no_ip": "আপনার IP ঠিকানা পাওয়া যায়নি। সাধারণ কানেকশন থেকে আবার চেষ্টা করুন।",
        "ip_used": "এই ডিভাইস (IP) ইতিমধ্যে ফ্রি প্রিমিয়াম নিয়েছে।",
        "dev_used": "এই ডিভাইস ইতিমধ্যে ফ্রি প্রিমিয়াম নিয়েছে।",
        "success": "\U0001F389 অভিনন্দন! আপনার অ্যাকাউন্টে ৩ দিনের প্রিমিয়াম চালু হয়েছে।",
        "notify": "\U0001F389 আপনার ৩ দিনের প্রিমিয়াম এখন চালু!\n\u23F0 মেয়াদ: {until}\n\n"
                  "আনলিমিটেড ফেভারিট, ইমেজ, বাটন, রঙ ও চ্যানেল উপভোগ করুন।",
    },
    "hi": {
        "title": "3 दिन का प्रीमियम लें", "heading": "3 दिन का प्रीमियम",
        "subtitle": "अपने टेलीग्राम खाते पर 3 दिन का प्रीमियम सक्रिय करने के लिए नीचे बटन दबाएँ।",
        "button": "अभी लें", "already": "इस डिवाइस पर मुफ़्त प्रीमियम पहले ही लिया जा चुका है।",
        "note": "एक डिवाइस पर एक ही बार मुफ़्त प्रीमियम। VPN और क्लोन ऐप मान्य नहीं हैं।",
        "unknown": "यह टेलीग्राम खाता बॉट को अभी ज्ञात नहीं है। पहले बॉट शुरू करें।",
        "claimed_acc": "इस खाते ने पहले ही मुफ़्त प्रीमियम ले लिया है।",
        "vpn": "VPN / प्रॉक्सी पाई गई। VPN से मुफ़्त प्रीमियम नहीं लिया जा सकता।",
        "no_ip": "आपका IP पता नहीं पढ़ा जा सका। सामान्य कनेक्शन से फिर प्रयास करें।",
        "ip_used": "इस डिवाइस (IP) ने पहले ही मुफ़्त प्रीमियम ले लिया है।",
        "dev_used": "इस डिवाइस ने पहले ही मुफ़्त प्रीमियम ले लिया है।",
        "success": "\U0001F389 बधाई! आपके खाते पर 3 दिन का प्रीमियम सक्रिय हो गया है।",
        "notify": "\U0001F389 आपका 3 दिन का प्रीमियम अब सक्रिय है!\n\u23F0 वैधता: {until}\n\n"
                  "असीमित पसंदीदा, इमेज, बटन, रंग और चैनल का आनंद लें।",
    },
    "ar": {
        "title": "احصل على بريميوم 3 أيام", "heading": "بريميوم 3 أيام",
        "subtitle": "اضغط الزر أدناه لتفعيل 3 أيام من البريميوم على حساب تيليجرام الخاص بك.",
        "button": "احصل الآن", "already": "تم الحصول على البريميوم المجاني بالفعل على هذا الجهاز.",
        "note": "بريميوم مجاني واحد لكل جهاز. VPN والتطبيقات المستنسخة غير مؤهلة.",
        "unknown": "هذا الحساب غير معروف للبوت بعد. ابدأ البوت أولاً.",
        "claimed_acc": "هذا الحساب حصل بالفعل على البريميوم المجاني.",
        "vpn": "تم اكتشاف VPN / وكيل. لا يمكن الحصول على البريميوم المجاني عبر VPN.",
        "no_ip": "تعذر قراءة عنوان IP. حاول مرة أخرى من اتصال عادي.",
        "ip_used": "هذا الجهاز (IP) حصل بالفعل على البريميوم المجاني.",
        "dev_used": "هذا الجهاز حصل بالفعل على البريميوم المجاني.",
        "success": "\U0001F389 تهانينا! تم تفعيل 3 أيام من البريميوم على حسابك.",
        "notify": "\U0001F389 بريميوم 3 أيام أصبح نشطًا الآن!\n\u23F0 صالح حتى: {until}\n\n"
                  "استمتع بمفضلات وصور وأزرار وألوان وقنوات غير محدودة.",
    },
}


def _claim_lang(user):
    lang = (user or {}).get("lang") if isinstance(user, dict) else None
    return lang if lang in CLAIM_WEB else "en"


CLAIM_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ t.title }}</title><style>{{ css|safe }}
.claim{max-width:460px;margin:10vh auto;background:linear-gradient(160deg,#141b33,#10162a);border:1px solid #232c49;border-radius:20px;padding:34px;text-align:center}
.claim h1{font-size:24px;margin-bottom:10px}
.claim p{color:#9aa7c7;font-size:14px;line-height:1.6;margin-bottom:18px}
.gift{font-size:52px;margin-bottom:12px}
.claim button{width:100%;padding:14px;font-size:16px;border-radius:12px}
.note{font-size:12px;color:#7d8ab0;margin-top:18px}
</style></head><body>
<div class="claim">
<div class="gift">\U0001F381</div>
<h1>{{ t.heading }}</h1>
{% if message %}<div class="flash {{ 'ok' if ok else 'err' }}">{{ message }}</div>{% endif %}
{% if not done %}
<p>{{ t.subtitle }}</p>
<form method="post" action="{{ url_for('web_claim_submit', user_id=user_id) }}">
<input type="hidden" name="device" value="{{ device }}">
<button type="submit">\U0001F48E {{ t.button }}</button></form>
{% else %}
<p>{{ t.already }}</p>
{% endif %}
<div class="note">{{ t.note }}</div>
</div></body></html>"""


def _claim_hash(value):
    salt = os.getenv("CLAIM_SALT") or get_setting("claim_salt") or "premium-emoji-bot"
    return hashlib.sha256(f"{salt}|{value}".encode("utf-8")).hexdigest()


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _proxy_detected():
    """Cheap VPN/proxy fingerprint check (headers set by proxies and tunnels)."""
    if get_setting("block_vpn") in {"0", "false", "no"}:
        return False
    # NB: X-Real-Ip is deliberately excluded — ordinary nginx setups set it
    # for every visitor and it would wrongly flag legitimate users as VPN.
    for header in ("Via", "Proxy-Connection", "X-Proxy-Id", "Forwarded"):
        if request.headers.get(header):
            return True
    return False


def _claim_state(user_id):
    with db() as conn:
        user = conn.execute(
            "SELECT user_id, first_name, username, lang, premium_claimed FROM users WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        claim = conn.execute(
            "SELECT * FROM premium_claims WHERE user_id=?", (int(user_id),)
        ).fetchone()
    claimed = bool(claim) or bool(user and int(user["premium_claimed"] or 0))
    return (dict(user) if user else None), claimed


@web_app.route("/claim_premium/<int:user_id>")
def web_claim_page(user_id):
    user, claimed = _claim_state(user_id)
    device = request.cookies.get("peb_device") or secrets.token_hex(16)
    body = render_template_string(
        CLAIM_TEMPLATE, css=WEB_CSS, user_id=user_id, user=user, t=CLAIM_WEB[_claim_lang(user)],
        done=claimed, message=None, ok=False, device=device,
    )
    response = web_app.make_response(body)
    if not request.cookies.get("peb_device"):
        response.set_cookie("peb_device", device, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


@web_app.route("/claim_premium/<int:user_id>", methods=["POST"])
def web_claim_submit(user_id):
    user, claimed = _claim_state(user_id)
    device = request.cookies.get("peb_device") or request.form.get("device") or ""
    ip = _client_ip()
    t = CLAIM_WEB[_claim_lang(user)]

    def page(message, ok=False):
        body = render_template_string(
            CLAIM_TEMPLATE, css=WEB_CSS, user_id=user_id, user=user, t=t,
            done=True, message=message, ok=ok, device=device,
        )
        response = web_app.make_response(body)
        if device and not request.cookies.get("peb_device"):
            response.set_cookie("peb_device", device, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return response

    if not user:
        return page(t["unknown"])
    if claimed:
        return page(t["claimed_acc"])
    if _proxy_detected():
        return page(t["vpn"])
    if not ip:
        return page(t["no_ip"])

    ip_hash = _claim_hash("ip:" + ip)
    device_hash = _claim_hash("dev:" + device) if device else ""
    with db() as conn:
        if conn.execute("SELECT 1 FROM premium_claims WHERE ip_hash=?", (ip_hash,)).fetchone():
            return page(t["ip_used"])
        if device_hash and conn.execute(
            "SELECT 1 FROM premium_claims WHERE device_hash=?", (device_hash,)
        ).fetchone():
            return page(t["dev_used"])
        conn.execute(
            "INSERT INTO premium_claims(user_id, ip_hash, device_hash) VALUES(?,?,?)",
            (int(user_id), ip_hash, device_hash),
        )
        conn.execute("UPDATE users SET premium_claimed=1, onboarded=1 WHERE user_id=?", (int(user_id),))
    grant_premium(int(user_id), 3)
    notify_user_markup(
        int(user_id),
        t["notify"].format(until=premium_until_text(user_id)),
        main_menu(int(user_id)),
    )
    return page(t["success"], ok=True)


@web_app.route("/health")
def web_health():
    return jsonify({"ok": True})


def start_web_dashboard():
    """Start the embedded Flask dashboard in a background thread."""
    if os.getenv("WEB_ENABLED", "1") in {"0", "false", "False", "no"}:
        return
    try:
        port = int(os.getenv("WEB_PORT", "8080"))
        thread = threading.Thread(
            target=lambda: web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
            daemon=True,
        )
        thread.start()
        print("=" * 62)
        print("  WEB DASHBOARD")
        print(f"  URL      : http://localhost:{port}")
        print(f"  Password : {WEB_PASSWORD}")
        print("=" * 62)
    except Exception as exc:
        print("Web dashboard failed to start:", repr(exc))


async def post_init(app: Application):
    global BOT_APP, BOT_LOOP
    BOT_APP = app
    BOT_LOOP = asyncio.get_running_loop()
    init_db()
    install_outgoing_localizer(app)
    start_web_dashboard()
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError("Telegram user login is not configured. Run python setup_login.py first.")
    print("Telegram user session connected.")
    print("Emoji ID Manager Bot is running!")


async def post_shutdown(app: Application):
    if user_client.is_connected():
        await user_client.disconnect()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cng", cng_command))
    app.add_handler(CommandHandler("cng_emoji", cng_emoji_command))
    app.add_handler(CommandHandler("cng_calel", cng_color_command))
    app.add_handler(CommandHandler("cng_color", cng_color_command))
    app.add_handler(CommandHandler("cng_order", cng_order_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("edit_button", edit_button_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/p\d+$"), p_page_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION | filters.PHOTO) & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
