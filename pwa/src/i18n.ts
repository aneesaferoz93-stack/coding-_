/**
 * i18n.ts — Pashto / Urdu / English strings for the field app.
 *
 * Pashto is first because that is the working language of the Patwaris who will
 * use this. The interface is RTL for Pashto and Urdu; numbers and CNIC entry
 * stay LTR, because flipping a 13-digit identity number is how transcription
 * errors get into the record.
 */

export type Lang = 'ps' | 'ur' | 'en';

export const LANGS: { code: Lang; label: string; dir: 'rtl' | 'ltr' }[] = [
  { code: 'ps', label: 'پښتو', dir: 'rtl' },
  { code: 'ur', label: 'اردو', dir: 'rtl' },
  { code: 'en', label: 'English', dir: 'ltr' },
];

type Dict = Record<string, string>;

const ps: Dict = {
  app_title: 'د ځمکې تصدیق',
  app_subtitle: 'د KP د ځمکې بشپړتیا سیستم',
  tab_visit: 'د ساحې معاینه',
  tab_queue: 'د لېږد کتار',
  tab_map: 'نقشه',
  offline: 'آف لائن',
  online: 'آن لائن',
  pending: 'نا استول شوي',
  sync_now: 'اوس استول',
  syncing: 'استول کېږي…',
  synced: 'واستول شو',
  device_not_enrolled: 'دا آله لا نه ده ثبت شوې',
  enrol_title: 'د آلې ثبتول',
  enrol_help:
    'تحصیلدار باید دا موبایل ستاسې په نوم ثبت کړي. د ثبت کوډ د یو ځل لپاره ښودل کېږي.',
  officer_id: 'د مامور پېژندګلو',
  device_label: 'د آلې نوم',
  enrol_button: 'آله ثبت کړئ',
  select_mouza: 'موضع غوره کړئ',
  select_assignment: 'کار غوره کړئ',
  khasra: 'خسره',
  khewat: 'خېوټ',
  area: 'مساحت',
  capture_gps: 'د GPS موقعیت واخلئ',
  capturing_gps: 'موقعیت اخیستل کېږي…',
  gps_ok: 'موقعیت واخیستل شو',
  gps_poor: 'موقعیت کمزوری دی',
  gps_failed: 'موقعیت ونه موندل شو',
  take_photo: 'انځور واخلئ',
  retake_photo: 'انځور بیا واخلئ',
  photo_required: 'ژوندی انځور اړین دی',
  gallery_blocked: 'د ګېلري نه انځور نه قبلېږي — یوازې کمره',
  witnesses: 'دوه ګاونډیان',
  witness_cnic: 'د ګاونډي شناختي کارډ',
  add_witness: 'ګاونډی ور زیات کړئ',
  notes: 'یادښتونه',
  submit: 'واستوئ',
  submit_queued: 'په کتار کې خوندي شو',
  accuracy: 'دقت',
  satellites: 'سپوږمکۍ',
  captured_at: 'د اخیستلو وخت',
  trust_score: 'د باور نمرې',
  map_title: 'د موضع پوله',
  inside_boundary: 'د پولې دننه',
  outside_boundary: 'د پولې بهر',
  boundary_unknown: 'پوله نه ده ډیجیټل شوې',
  boundary_unknown_help:
    'د دې موضع نقشه لا نه ده جوړه شوې، نو ځای په اتومات ډول نه شي تاییدېدای. خپل کار ته دوام ورکړئ — دا به د بیا کتنې لپاره په نښه شي.',
  required_actions: 'اړین ګامونه',
  flags: 'نښې',
  status: 'حالت',
  clear: 'صاف',
  hold: 'تر غور لاندې',
  block: 'بند',
  advisory: 'مشوره',
  no_flags: 'هیڅ نښه نشته',
  queue_empty: 'کتار تش دی',
  delete: 'ړنګول',
  confirm_delete: 'ډاډه یاست؟',
  view_result: 'پایله وګورئ',
  server_said: 'سرور وویل',
  proof_hash: 'د ثبوت هش',
  language: 'ژبه',
  help: 'مرسته',
  help_text:
    '۱. د کروندې په ځای ودرېږئ. ۲. GPS واخلئ. ۳. د کمرې نه ژوندی انځور واخلئ. ۴. د دوه ګاونډیانو شناختي کارډونه ولیکئ. ۵. واستوئ. که انټرنېټ نه وي، کار به خوندي شي او وروسته به واستول شي.',
  danger_help:
    'که تاسې ته د درغلۍ په اړه مالومات رسېدلي وي، نو تحصیلدار ته خبر ورکړئ. دا سیستم ستاسې د ساتنې لپاره دی، ستاسې د تعقیب لپاره نه.',
};

const ur: Dict = {
  app_title: 'زمین کی تصدیق',
  app_subtitle: 'KP لینڈ ریکارڈ انٹیگریٹی سسٹم',
  tab_visit: 'موقع کا معائنہ',
  tab_queue: 'ارسال کی قطار',
  tab_map: 'نقشہ',
  offline: 'آف لائن',
  online: 'آن لائن',
  pending: 'ارسال باقی',
  sync_now: 'ابھی بھیجیں',
  syncing: 'بھیجا جا رہا ہے…',
  synced: 'بھیج دیا گیا',
  device_not_enrolled: 'یہ آلہ ابھی رجسٹرڈ نہیں ہے',
  enrol_title: 'آلہ رجسٹر کریں',
  enrol_help:
    'تحصیلدار کو یہ موبائل آپ کے نام رجسٹر کرنا ہوگا۔ رجسٹریشن کوڈ صرف ایک بار دکھایا جاتا ہے۔',
  officer_id: 'اہلکار شناخت',
  device_label: 'آلے کا نام',
  enrol_button: 'آلہ رجسٹر کریں',
  select_mouza: 'موضع منتخب کریں',
  select_assignment: 'کام منتخب کریں',
  khasra: 'خسرہ',
  khewat: 'خروٹ',
  area: 'رقبہ',
  capture_gps: 'GPS مقام لیں',
  capturing_gps: 'مقام لیا جا رہا ہے…',
  gps_ok: 'مقام مل گیا',
  gps_poor: 'مقام کمزور ہے',
  gps_failed: 'مقام نہیں ملا',
  take_photo: 'تصویر لیں',
  retake_photo: 'دوبارہ تصویر لیں',
  photo_required: 'لائیو تصویر ضروری ہے',
  gallery_blocked: 'گیلری سے تصویر قبول نہیں — صرف کیمرہ',
  witnesses: 'دو پڑوسی',
  witness_cnic: 'پڑوسی کا شناختی کارڈ',
  add_witness: 'پڑوسی شامل کریں',
  notes: 'نوٹس',
  submit: 'بھیجیں',
  submit_queued: 'قطار میں محفوظ ہوا',
  accuracy: 'درستی',
  satellites: 'سیٹلائٹ',
  captured_at: 'وقت',
  trust_score: 'اعتماد اسکور',
  map_title: 'موضع کی حدود',
  inside_boundary: 'حد کے اندر',
  outside_boundary: 'حد سے باہر',
  boundary_unknown: 'حد ڈیجیٹل نہیں',
  boundary_unknown_help:
    'اس موضع کا نقشہ ابھی نہیں بنایا گیا، اس لیے مقام خودکار طور پر تصدیق نہیں ہو سکتا۔ اپنا کام جاری رکھیں — یہ جائزے کے لیے نشان زد ہوگا۔',
  required_actions: 'ضروری اقدامات',
  flags: 'نشانات',
  status: 'حیثیت',
  clear: 'صاف',
  hold: 'زیرِ غور',
  block: 'بند',
  advisory: 'مشورہ',
  no_flags: 'کوئی نشان نہیں',
  queue_empty: 'قطار خالی ہے',
  delete: 'حذف کریں',
  confirm_delete: 'کیا آپ پختہ ہیں؟',
  view_result: 'نتیجہ دیکھیں',
  server_said: 'سرور نے کہا',
  proof_hash: 'ثبوت ہیش',
  language: 'زبان',
  help: 'مدد',
  help_text:
    '۱. کھیت پر کھڑے ہوں۔ ۲. GPS لیں۔ ۳. کیمرے سے لائیو تصویر لیں۔ ۴. دو پڑوسیوں کے شناختی کارڈ درج کریں۔ ۵. بھیجیں۔ اگر انٹرنیٹ نہ ہو تو کام محفوظ ہوگا اور بعد میں بھیج دیا جائے گا۔',
  danger_help:
    'اگر آپ کو دھوکہ دہی کا علم ہو تو تحصیلدار کو اطلاع دیں۔ یہ نظام آپ کے تحفظ کے لیے ہے، آپ کی نگرانی کے لیے نہیں۔',
};

const en: Dict = {
  app_title: 'Ground Verification',
  app_subtitle: 'KP Land Record Integrity System',
  tab_visit: 'Site Visit',
  tab_queue: 'Sync Queue',
  tab_map: 'Map',
  offline: 'Offline',
  online: 'Online',
  pending: 'Pending',
  sync_now: 'Sync now',
  syncing: 'Syncing…',
  synced: 'Synced',
  device_not_enrolled: 'This handset is not enrolled yet',
  enrol_title: 'Enrol this handset',
  enrol_help:
    'A Tehsildar must register this phone to your name. The enrolment secret is shown once.',
  officer_id: 'Officer ID',
  device_label: 'Device label',
  enrol_button: 'Enrol device',
  select_mouza: 'Select mouza',
  select_assignment: 'Select assignment',
  khasra: 'Khasra',
  khewat: 'Khewat',
  area: 'Area',
  capture_gps: 'Capture GPS',
  capturing_gps: 'Acquiring fix…',
  gps_ok: 'Fix acquired',
  gps_poor: 'Fix is weak',
  gps_failed: 'Could not get a fix',
  take_photo: 'Take photo',
  retake_photo: 'Retake photo',
  photo_required: 'A live photo is required',
  gallery_blocked: 'Gallery images are not accepted — camera only',
  witnesses: 'Two neighbours',
  witness_cnic: 'Neighbour CNIC',
  add_witness: 'Add neighbour',
  notes: 'Notes',
  submit: 'Submit',
  submit_queued: 'Saved to the queue',
  accuracy: 'Accuracy',
  satellites: 'Satellites',
  captured_at: 'Captured',
  trust_score: 'Trust score',
  map_title: 'Mouza boundary',
  inside_boundary: 'Inside boundary',
  outside_boundary: 'Outside boundary',
  boundary_unknown: 'Boundary not digitised',
  boundary_unknown_help:
    'This mouza has no digitised cadastral map yet, so the fix cannot be automatically placed on the parcel. Continue your work — the record will be marked for review.',
  required_actions: 'Required actions',
  flags: 'Flags',
  status: 'Status',
  clear: 'Clear',
  hold: 'Under review',
  block: 'Blocked',
  advisory: 'Advisory',
  no_flags: 'No flags',
  queue_empty: 'Queue is empty',
  delete: 'Delete',
  confirm_delete: 'Are you sure?',
  view_result: 'See result',
  server_said: 'Server said',
  proof_hash: 'Proof hash',
  language: 'Language',
  help: 'Help',
  help_text:
    '1. Stand on the parcel. 2. Capture GPS. 3. Take a live camera photo. 4. Enter two neighbours’ CNICs. 5. Submit. With no signal the visit is stored on the phone and sent later.',
  danger_help:
    'If you have been told about a fraudulent transfer, report it to your Tehsildar. This system exists to protect you, not to watch you.',
};

const DICTS: Record<Lang, Dict> = { ps, ur, en };

const STORAGE_KEY = 'klri.lang';

export function detectLang(): Lang {
  const saved = localStorage.getItem(STORAGE_KEY) as Lang | null;
  if (saved && saved in DICTS) return saved;
  const nav = navigator.language.toLowerCase();
  if (nav.startsWith('ps')) return 'ps';
  if (nav.startsWith('ur')) return 'ur';
  return 'ps'; // default for KP field staff
}

export function setLang(lang: Lang) {
  localStorage.setItem(STORAGE_KEY, lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = LANGS.find((l) => l.code === lang)?.dir ?? 'rtl';
}

export function t(lang: Lang, key: string): string {
  return DICTS[lang][key] ?? DICTS.en[key] ?? key;
}

export function makeT(lang: Lang) {
  return (key: string) => t(lang, key);
}
