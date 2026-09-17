# services/identity.py
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Set, Tuple, List, Dict, Any


@dataclass
class TrackIdentity:
    """Полная идентичность музыкального трека."""
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None
    source_platform: str = "unknown"
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    canonical_artist: Optional[str] = None
    canonical_title: Optional[str] = None
    canonical_album: Optional[str] = None
    canonical_duration: Optional[int] = None
    is_search: bool = False

    @property
    def display_artist(self) -> str:
        return self.canonical_artist or self.artist or "Unknown Artist"

    @property
    def display_title(self) -> str:
        return self.canonical_title or self.title or "Unknown Track"

    @property
    def display_name(self) -> str:
        return f"{self.display_artist} — {self.display_title}"


@dataclass
class VariantIdentity:
    """Идентичность версии/модификатора трека."""
    requested_modifiers: Set[str] = field(default_factory=set)
    source_modifiers: Set[str] = field(default_factory=set)
    applied_modifiers: Set[str] = field(default_factory=set)
    speed_multiplier: Optional[float] = None
    version_markers: Set[str] = field(default_factory=set)

    @property
    def variant_key(self) -> str:
        """Детерминированный строковый ключ для кэша и сопоставления."""
        parts = []
        if self.speed_multiplier:
            parts.append(f"{self.speed_multiplier:.2f}x")
        if self.requested_modifiers:
            parts.extend(sorted(self.requested_modifiers))
        return "+".join(parts) if parts else "original"


# -------------------------------------------------------------
# 1. Unicode Sanitization & Normalization
# -------------------------------------------------------------

_ZERO_WIDTH_RE = re.compile(
    r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u202a-\u202e\u2066-\u2069]'
)


def clean_unicode_text(text: Optional[str]) -> str:
    """
    Безопасная нормализация пользовательского текста:
    - NFKC нормализация;
    - удаление zero-width space/joiner, bidi controls;
    - замена типографских апострофов и кавычек;
    - сохранение легитимных Unicode символов (кириллица, азиатские языки и др.).
    """
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    cleaned = _ZERO_WIDTH_RE.sub("", norm)
    cleaned = cleaned.replace('\xa0', ' ')
    cleaned = cleaned.replace("’", "'").replace("‘", "'").replace("`", "'")
    cleaned = cleaned.replace("“", '"').replace("”", '"')
    return " ".join(cleaned.split())


# -------------------------------------------------------------
# 2. Vocabulary & Modifier Definitions
# -------------------------------------------------------------

TRACK_MODIFIERS = {
    # Speed / Pitch
    "super slowed down", "super slowed", "super slow", "ultra slowed",
    "slowed down", "slowed", "slower", "slow version", "reverb", "reverbed",
    "slowed + reverb", "slowed & reverb", "slowed and reverb", "slowed+reverb",
    "slowed reverb", "slowedreverb",
    "chopped and screwed", "chopped & screwed", "chopped + screwed", "chopped screwed",
    "low pitch", "pitch down", "pitched down", "down pitch",
    "high pitch", "pitch up", "pitched up",
    "speed up", "speedup", "sped up", "spedup", "fast version", "sped up + reverb",
    "speed_multiplier",
    # Versions & Edits
    "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix",
    "cover", "кавер", "acoustic", "акустика", "piano", "пианино",
    "acapella", "a cappella", "акапелла", "лайв", "концерт",
    "8d", "16d", "nightcore", "daycore", "instrumental", "инструментал", "minus", "минус",
    "edit", "fan edit", "karaoke", "караоке", "orchestral", "orchestra", "tribute",
    "drum edit", "drum cover", "drum version", "drum rework", "drum remix", "drum beat",
    "with drums", "with drum", "drums version", "drum and bass", "drum & bass", "dnb",
    "драмка", "с драмкой", "с барабанами", "днб",
    "rework", "drill", "дрил", "дрилл", "phonk", "фонк", "jersey club",
    "club mix", "club edit", "club version", "bass boost", "bass boosted", "808", "type beat",
    "mix", "микс", "dj mix", "radio edit", "extended mix", "extended version", "dance mix",
    # Additional Section 6 Modifiers:
    "clean", "explicit", "clean version", "explicit version",
    "remaster", "remastered", "re-mastered", "re-recorded", "re recorded",
    "demo", "demo version", "session", "unplugged", "acoustic session", "live session"
}

PERFORMANCE_MODIFIERS = {
    "america's got talent", "agt", "the voice", "snl", "saturday night live",
    "tonight show", "jimmy fallon", "jimmy kimmel", "james corden", "colbert",
    "tiny desk", "colors show", "a colors show", "bbc radio 1 live lounge", "live lounge",
    "grammy", "grammys", "grammy awards", "vma", "vmas", "billboard music awards",
    "bbmas", "amas", "american music awards", "brit awards", "oscar", "oscars",
    "super bowl", "halftime show", "halftime",
    "live at", "live in", "live from", "live @", "en vivo", "dal vivo", "ao vivo",
    "live performance", "stage performance", "stage mix", "live version",
    "acoustic session", "live session", "unplugged"
}

# DSP-модификация аудио через FFmpeg полностью отключена:
# Все пользовательские модификаторы требуют поиска готовой версии (Ready-Made Variants)
DSP_SUPPORTED_MODIFIERS = set()

SEMANTIC_MODIFIERS = {
    # Tempo & Pitch variants (теперь требуют готовый релиз, без DSP):
    "slowed", "super slowed", "ultra slowed", "slowed down", "slow version", "slower", "slow",
    "sped up", "speed up", "speedup", "spedup", "fast version", "speed_multiplier",
    "reverb", "reverbed", "slowed + reverb", "slowed & reverb", "nightcore", "daycore",
    # Versions & Edits:
    "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix", "club mix", "dance mix",
    "live", "лайв", "концерт", "performance", "acoustic", "акустика", "unplugged",
    "cover", "кавер", "tribute", "karaoke", "караоке",
    "instrumental", "инструментал", "minus", "минус", "acapella", "a cappella",
    "clean", "explicit", "remaster", "remastered", "demo"
}

SUPER_SLOWED_GROUP: Set[str] = {"super slowed down", "super slowed", "super slow", "ultra slowed"}
SLOWED_GROUP: Set[str] = {"slowed down", "slowed", "slower", "slow version", "slow"}
SPED_UP_GROUP: Set[str] = {"speed up", "speedup", "sped up", "spedup", "fast version", "speed_multiplier"}
REVERB_GROUP: Set[str] = {"reverb", "reverbed", "slowed + reverb", "slowed & reverb", "slowed and reverb", "slowed+reverb", "slowed reverb", "slowedreverb", "sped up + reverb"}
NIGHTCORE_GROUP: Set[str] = {"nightcore"}
DAYCORE_GROUP: Set[str] = {"daycore"}
ACOUSTIC_GROUP: Set[str] = {"acoustic", "акустика", "piano", "пианино", "unplugged"}
LIVE_GROUP: Set[str] = {"live", "лайв", "концерт", "performance"}
COVER_GROUP: Set[str] = {"cover", "кавер", "tribute"}
REMIX_GROUP: Set[str] = {"remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix", "club mix", "dance mix"}
INSTRUMENTAL_GROUP: Set[str] = {"instrumental", "инструментал", "minus", "минус", "karaoke", "караоке"}
ACAPELLA_GROUP: Set[str] = {"acapella", "a cappella", "акапелла"}
BASS_BOOST_GROUP: Set[str] = {"bass boost", "bass boosted"}
EIGHT_D_GROUP: Set[str] = {"8d", "16d"}


def is_candidate_matching_modifiers(requested_modifiers: Set[str], cand_modifiers: Set[str]) -> bool:
    """
    Проверяет, удовлетворяет ли найденный кандидат запрошенным модификаторам:
    - Super Slowed: строго требует super slowed, обычный slowed НЕ считается совпадением;
    - Slowed: требует slowed или super slowed;
    - Sped Up: требует ускоренную версию;
    - Reverb: требует наличие реверберации;
    - Для комбинаций (например Slowed + Reverb): требует удовлетворения ВСЕХ запрошенных компонентов;
    - Кандидат-оригинал без модификаторов отклоняется, если запрошен хотя бы один модификатор.
    """
    if not requested_modifiers:
        return True

    cand_mods = set(cand_modifiers) if cand_modifiers else set()
    req_mods = set(requested_modifiers)

    # 1. Super Slowed: строго требует super slowed, обычный slowed не считается совпадением
    if req_mods & SUPER_SLOWED_GROUP:
        if not (cand_mods & SUPER_SLOWED_GROUP):
            return False
    # 2. Slowed (если super slowed НЕ запрашивался): требует slowed или super slowed
    elif req_mods & SLOWED_GROUP:
        if not (cand_mods & (SLOWED_GROUP | SUPER_SLOWED_GROUP)):
            return False

    # 3. Sped Up:
    if req_mods & SPED_UP_GROUP:
        if not (cand_mods & SPED_UP_GROUP):
            return False

    # 4. Reverb:
    if req_mods & REVERB_GROUP:
        if not (cand_mods & REVERB_GROUP):
            return False

    # 5. Nightcore:
    if req_mods & NIGHTCORE_GROUP:
        if not (cand_mods & NIGHTCORE_GROUP):
            return False

    # 6. Daycore:
    if req_mods & DAYCORE_GROUP:
        if not (cand_mods & DAYCORE_GROUP):
            return False

    # 7. Прочие группы (acoustic, live, remix, instrumental и др.)
    for grp in [ACOUSTIC_GROUP, LIVE_GROUP, COVER_GROUP, REMIX_GROUP, INSTRUMENTAL_GROUP, ACAPELLA_GROUP, BASS_BOOST_GROUP, EIGHT_D_GROUP]:
        if req_mods & grp:
            if not (cand_mods & grp):
                return False

    # 8. Любые другие модификаторы:
    handled_groups = (
        SUPER_SLOWED_GROUP | SLOWED_GROUP | SPED_UP_GROUP | REVERB_GROUP |
        NIGHTCORE_GROUP | DAYCORE_GROUP | ACOUSTIC_GROUP | LIVE_GROUP |
        COVER_GROUP | REMIX_GROUP | INSTRUMENTAL_GROUP | ACAPELLA_GROUP |
        BASS_BOOST_GROUP | EIGHT_D_GROUP
    )
    other_req = req_mods - handled_groups
    for m in other_req:
        if m not in cand_mods:
            return False

    return True


# -------------------------------------------------------------
# 3. Speed Multiplier Parsing
# -------------------------------------------------------------

_SPEED_MULT_RE = re.compile(
    r'(?<![\w.])(0\.[5-9]\d?|1\.\d{1,2}|2(?:\.0)?)\s*x(?![\w.])',
    re.IGNORECASE
)
_SPEED_PERCENT_RE = re.compile(
    r'(?<![\w.])([5-9]\d|1[0-9]\d|200)\s*%\s*(?:speed|скорость)?(?![\w.])',
    re.IGNORECASE
)


def parse_speed_multiplier(text: Optional[str]) -> Optional[float]:
    """
    Извлекает числовой множитель скорости из текста в диапазоне 0.5x .. 2.0x.
    """
    if not text:
        return None
    cleaned = clean_unicode_text(text)

    m = _SPEED_MULT_RE.search(cleaned)
    if m:
        try:
            val = float(m.group(1))
            if 0.5 <= val <= 2.0 and val != 1.0:
                return round(val, 2)
        except ValueError:
            pass

    m_pct = _SPEED_PERCENT_RE.search(cleaned)
    if m_pct:
        try:
            pct = float(m_pct.group(1))
            val = pct / 100.0
            if 0.5 <= val <= 2.0 and val != 1.0:
                return round(val, 2)
        except ValueError:
            pass

    return None


# -------------------------------------------------------------
# 4. Modifier Extraction
# -------------------------------------------------------------

def extract_modifiers(text: Optional[str], ignore_words: Optional[Set[str]] = None) -> Set[str]:
    """
    Извлекает множество модификаторов трека с учетом границ слов и контекста.
    """
    if not text:
        return set()
    cleaned = clean_unicode_text(text).lower()
    found = set()
    norm_ignores = {clean_unicode_text(w).lower() for w in ignore_words} if ignore_words else set()

    for mod in TRACK_MODIFIERS:
        pattern = r'(?<!\w)' + re.escape(mod) + r'(?!\w)'
        if re.search(pattern, cleaned):
            if norm_ignores and mod in norm_ignores:
                continue
            found.add(mod)

    # Контекстная проверка слова "slow"
    if not ("slow" in norm_ignores or "slowed" in found or "super slow" in found or "slow version" in found):
        is_slow_mod = False
        if re.search(r'[\(\[\{][^\)\]\}]*\bslow\b[^\)\]\}]*[\)\]\}]', cleaned):
            is_slow_mod = True
        elif re.search(r'[\-_/|]\s*slow[\s.,!]*$', cleaned):
            is_slow_mod = True
        elif re.search(r'\bslow\s*(?:[+&]|and)?\s*(?:reverb|pitch|edit|version|down)\b', cleaned):
            is_slow_mod = True
        elif re.search(r'(?:[\s\-_/|]|^)slow[\s.,!]*$', cleaned):
            if not re.search(r'\b(?:go|walk|talk|drive|move|run|too)\s+slow[\s.,!]*$', cleaned):
                is_slow_mod = True
        if is_slow_mod:
            found.add("slow")

    # Контекстная проверка слова "live"
    if not ("live" in norm_ignores or "лайв" in found or "концерт" in found):
        is_live_mod = False
        if re.search(r'[\(\[\{][^\)\]\}]*\blive\b[^\)\]\}]*[\)\]\}]', cleaned):
            is_live_mod = True
        elif re.search(r'[\-_/|]\s*live[\s.,!]*$', cleaned):
            is_live_mod = True
        elif re.search(r'\blive\s+(?:at|in|from|@|acoustic|session|sessions|version|performance|concert|tour|recording|19\d\d|20\d\d)\b', cleaned):
            is_live_mod = True
        elif any(pm in cleaned for pm in PERFORMANCE_MODIFIERS):
            is_live_mod = True
            found.add("performance")
        elif re.search(r'(?:[\s\-_/|]|^)live[\s.,!]*$', cleaned):
            if not re.search(r'\b(?:to|i|we|you|they|he|she|it|do|shall|will|can|could|would|might|must|long|wanna|born\s+to|live\s+to)\s+live[\s.,!]*$', cleaned):
                is_live_mod = True
        if is_live_mod:
            found.add("live")

    # Числовой множитель
    if parse_speed_multiplier(cleaned) is not None:
        found.add("speed_multiplier")

    # Барабаны (только в контексте явной модификации, а не как самостоятельное слово артиста/трека)
    if not ("drum" in norm_ignores or "drums" in norm_ignores or "барабаны" in norm_ignores or "барабан" in norm_ignores):
        is_drum_mod = False
        if re.search(r'[\(\[\{][^\)\]\}]*\b(?:drums?|барабан[ыа]?)\b[^\)\]\}]*[\)\]\}]', cleaned):
            is_drum_mod = True
        elif re.search(r'\b(?:with|on|and|с|со|под)\s+(?:drums?|барабан(?:ами|ы|а)?)\b', cleaned):
            is_drum_mod = True
        elif re.search(r'\b(?:drums?|барабан[ыа]?)\s+(?:version|ver|edit|mix|remix|cover|rework|beat|solo|track|instrumental)\b', cleaned):
            is_drum_mod = True
        elif re.search(r'[\-_/|]\s*(?:drums?|барабан[ыа]?)[\s.,!]*$', cleaned):
            prefix = re.sub(r'[\-_/|]\s*(?:drums?|барабан[ыа]?)[\s.,!]*$', '', cleaned).strip()
            if prefix and len(prefix) > 2:
                is_drum_mod = True
        if is_drum_mod:
            found.add("drums")

    return found


def has_track_modifiers(text: Optional[str], ignore_words: Optional[Set[str]] = None) -> bool:
    return bool(extract_modifiers(text, ignore_words=ignore_words))


def extract_track_modifiers(text: Optional[str], ignore_words: Optional[Set[str]] = None) -> Tuple[str, List[str]]:
    """
    Извлекает список модификаторов из текста и возвращает очищенный текст без модификаторов:
    (clean_text, sorted_modifiers_list)
    """
    if not text:
        return "", []
    mods = extract_modifiers(text, ignore_words=ignore_words)
    if not mods:
        return text.strip(), []

    # Удаляем модификаторы из текста
    cleaned = text
    for m in sorted(mods, key=len, reverse=True):
        pattern = r'(?i)(?:[\s\-_/|(]|^)' + re.escape(m) + r'(?:[\s\-_/|)]|$)'
        cleaned = re.sub(pattern, ' ', cleaned)

    cleaned = re.sub(r'[\(\[\{]\s*[\)\]\}]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # Защита от стирания названия трека (например "The Drums - Drums" или "Live"):
    # если удаление модификаторов полностью стёрло текст, значит это было само название трека
    if not cleaned:
        return text.strip(), []

    return cleaned, sorted(list(mods))


# -------------------------------------------------------------
# 5. Transliteration & Layout
# -------------------------------------------------------------

TRANSLIT_TABLE = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
}


def transliterate_text(text: str) -> str:
    norm = clean_unicode_text(text).lower()
    return "".join(TRANSLIT_TABLE.get(c, c) for c in norm)


# -------------------------------------------------------------
# 6. Robust Artist Validation
# -------------------------------------------------------------

_ARTIST_SEPARATORS_RE = re.compile(
    r'\s*(?:feat\.?|ft\.?|featuring|with|prod\.?|prod\s+by|vs\.?|x|&|,|/|\\)\s*',
    re.IGNORECASE
)


def split_artist_names(artist_str: Optional[str]) -> Set[str]:
    """Разбивает строку исполнителей на отдельные имена."""
    if not artist_str:
        return set()
    cleaned = clean_unicode_text(artist_str)
    raw_artists = _ARTIST_SEPARATORS_RE.split(cleaned)
    result = set()
    for a in raw_artists:
        a_clean = a.strip()
        if a_clean:
            result.add(a_clean)
            no_the = re.sub(r'^(?:the|a|an)\s+', '', a_clean, flags=re.IGNORECASE).strip()
            if no_the and no_the != a_clean and no_the.lower() not in {"the", "a", "an"}:
                result.add(no_the)
    return result


def validate_artist_match(expected_artist: Optional[str], candidate_text: Optional[str]) -> bool:
    """
    Строгая валидация совпадения исполнителя:
    - проверяет точные нормализованные токены артиста в тексте кандидата (uploader/channel/title);
    - поддерживает мульти-артистов, feat, ft, &, запятые;
    - предотвращает ложные совпадения подстрок ("Eve" != "Steve Aoki", "Ian" != "Ariana Grande", "A" != "The A").
    """
    if not expected_artist or not candidate_text:
        return True

    clean_expected = clean_unicode_text(expected_artist).lower()
    clean_cand = clean_unicode_text(candidate_text).lower()

    if clean_expected in clean_cand:
        pattern = r'(?<![a-z0-9а-яё])' + re.escape(clean_expected) + r'(?![a-z0-9а-яё])'
        if re.search(pattern, clean_cand):
            return True

    expected_sub_artists = split_artist_names(expected_artist)
    for art in expected_sub_artists:
        art_norm = art.lower()
        if art_norm in {"the", "a", "an", "and", "or", "of", "in", "to"}:
            continue
        if len(art_norm) >= 2:
            pattern = r'(?<![a-z0-9а-яё])' + re.escape(art_norm) + r'(?![a-z0-9а-яё])'
            if re.search(pattern, clean_cand):
                return True
            art_tr = transliterate_text(art_norm)
            if art_tr != art_norm and len(art_tr) >= 2 and art_tr not in {"the", "a", "an"}:
                pattern_tr = r'(?<![a-z0-9а-яё])' + re.escape(art_tr) + r'(?![a-z0-9а-яё])'
                if re.search(pattern_tr, clean_cand):
                    return True

    return False


# -------------------------------------------------------------
# 7. Title Matching & Eponymous Protection
# -------------------------------------------------------------

def extract_core_title_words(title: Optional[str], artist: Optional[str] = None) -> Set[str]:
    """
    Извлекает ключевые слова названия трека с защитой от eponymous-запросов
    (The Drums - Drums, Bad Company - Bad Company, Future - Future).
    """
    if not title:
        return set()
    raw = clean_unicode_text(title).lower()

    # 1. Удаляем feat/ft/prod конструкции в скобках и без скобок (например 'Rich Flex ft. 21 Savage')
    raw = re.sub(r'[\(\[][^\)\]]*(?:feat\.?|ft\.?|featuring|prod\.?|prod\s+by)[^\)\]]*[\)\]]', ' ', raw)
    raw = re.sub(r'\b(?:feat\.?|ft\.?|featuring|prod\.?|prod\s+by)\b.*$', ' ', raw)
    raw = re.sub(r'\b(?:0\.[5-9]\d?|1\.\d{1,2}|2(?:\.0)?)\s*x\b', ' ', raw)
    raw = re.sub(r'\b(?:[5-9]\d|1[0-9]\d|200)\s*%\s*(?:speed|скорость)?\b', ' ', raw)
    for mod in sorted(TRACK_MODIFIERS | PERFORMANCE_MODIFIERS, key=len, reverse=True):
        raw = re.sub(r'(?<!\w)' + re.escape(mod) + r'(?!\w)', ' ', raw)

    raw_tokens = set(re.findall(r'[\w]+', raw))
    if artist:
        artist_clean = clean_unicode_text(artist).lower()
        artist_tokens = set(re.findall(r'[\w]+', artist_clean))
        if raw_tokens and raw_tokens.issubset(artist_tokens):
            return {t for t in raw_tokens if len(t) >= 1}
        for aw in artist_tokens:
            if len(aw) >= 2:
                raw = re.sub(r'(?<!\w)' + re.escape(aw) + r'(?!\w)', ' ', raw)

    tokens = set(re.findall(r'[\w]+', raw))
    return {t for t in tokens if len(t) >= 1}


def compute_title_match_ratio(cand_title: str, core_words: Set[str]) -> float:
    """
    Вычисляет коэффициент соответствия названия кандидата:
    - если core_words пустое, возвращает 0.0 (а не 1.0!);
    - штрафует лишние конфликтующие слова для коротких названий ('Song' vs 'Song 2', 'Stay' vs 'Stay Tonight');
    - различает Part 1 vs Part 2, римские цифры.
    """
    if not core_words or not cand_title:
        return 0.0

    cand_clean = clean_unicode_text(cand_title).lower()
    cand_translit = transliterate_text(cand_clean)
    cand_tokens = set(re.findall(r'[\w]+', cand_clean))
    cand_tokens_tr = set(re.findall(r'[\w]+', cand_translit))

    matched = 0
    for w in core_words:
        w_tr = transliterate_text(w)
        if w in cand_tokens or w_tr in cand_tokens_tr or w_tr in cand_tokens:
            matched += 1
            continue
        if len(w) >= 3 and (w in cand_clean or w_tr in cand_translit):
            matched += 1
            continue
        if len(w) >= 4:
            stem = w[:-1] if len(w) > 4 else w
            stem_tr = w_tr[:-1] if len(w_tr) > 4 else w_tr
            if any(stem in tok for tok in cand_tokens if len(tok) >= 4) or                any(stem_tr in tok for tok in cand_tokens_tr if len(tok) >= 4):
                matched += 1
                continue

    coverage = matched / len(core_words)

    part_query = re.findall(r'\b(?:pt|part|часть)\s*(\d+|[ivx]+)\b', " ".join(core_words))
    part_cand = re.findall(r'\b(?:pt|part|часть)\s*(\d+|[ivx]+)\b', cand_clean)
    if part_query and part_cand and part_query != part_cand:
        return 0.2

    # Для коротких названий из 1-2 слов проверяем числовые суффиксы и лишние слова в чистом названии
    if len(core_words) <= 2 and coverage == 1.0:
        # Если кандидат содержит разделитель "Артист - Название", берем правую часть
        tit_part = cand_clean
        for sep in [" — ", " - ", " – "]:
            if sep in cand_clean:
                tit_part = cand_clean.split(sep, 1)[1]
                break
        cand_mods = extract_modifiers(tit_part)
        mod_words = set()
        for m in cand_mods:
            mod_words.update(m.split())
        for m in TRACK_MODIFIERS:
            if m in tit_part:
                mod_words.update(m.split())
        filler_words = {"the", "a", "an", "official", "audio", "video", "topic", "visualizer", "lyrics", "lyric", "hq", "hd"}
        tit_tokens = set(re.findall(r'[\w]+', tit_part))
        tit_meaningful = {t for t in tit_tokens if len(t) > 2 and t not in filler_words and t not in mod_words}
        extra_tokens = tit_meaningful - core_words
        if len(extra_tokens) >= 2:
            coverage = 0.65

    return coverage


def parse_query_artist_title(query: str) -> Tuple[Optional[str], str]:
    """
    Разбивает пользовательский поисковый запрос на (artist, title):
    - Поддерживает разделители: ' — ', ' - ', ' – ', '—', '–', ' : ', ' / ', ' | ';
    - Защищает составные имена исполнителей с дефисами (A-Ha, Jay-Z, Blink-182, T-Pain, AC/DC);
    - Если разделителя нет, возвращает (None, query).
    """
    cleaned = clean_unicode_text(query)
    if not cleaned:
        return None, ""

    # Приоритет разделителям с пробелами вокруг, чтобы не разбивать A-Ha, Jay-Z, Blink-182
    spaced_separators = [" — ", " - ", " – ", " : ", " | ", " / "]
    for sep in spaced_separators:
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            if parts[0].strip() and parts[1].strip():
                return parts[0].strip(), parts[1].strip()

    # Длинные тире без пробелов (например: "Исполнитель—Название")
    for unspaced_dash in ["—", "–"]:
        if unspaced_dash in cleaned:
            parts = cleaned.split(unspaced_dash, 1)
            if parts[0].strip() and parts[1].strip():
                return parts[0].strip(), parts[1].strip()

    # Разделитель "Название by Артист" (Reversed Query)
    by_match = re.split(r'\s+\bby\b\s+', cleaned, maxsplit=1, flags=re.IGNORECASE)
    if len(by_match) == 2 and by_match[0].strip() and by_match[1].strip():
        return by_match[1].strip(), by_match[0].strip()

    return None, cleaned
