# services/identity.py
import functools
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


@functools.lru_cache(maxsize=4096)
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
    "cover", "кавер", "acoustic", "акустика", "киберакустика", "кибер акустика", "piano", "пианино",
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
    "live", "лайв", "концерт", "performance", "acoustic", "акустика", "киберакустика", "кибер акустика", "unplugged",
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
ACOUSTIC_GROUP: Set[str] = {"acoustic", "акустика", "киберакустика", "кибер акустика", "piano", "пианино", "unplugged"}
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


@functools.lru_cache(maxsize=1024)
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


@functools.lru_cache(maxsize=4096)
def transliterate_text(text: str) -> str:
    norm = clean_unicode_text(text).lower()
    return "".join(TRANSLIT_TABLE.get(c, c) for c in norm)


# -------------------------------------------------------------
# 6. Robust Artist Validation
# -------------------------------------------------------------

_ARTIST_SEPARATORS_RE = re.compile(
    r'\s*(?:(?:\b(?:feat|ft|featuring|with|prod|prod\s+by|vs)\b\.?)|(?:\s+x\s+)|[&,/\\|])\s*',
    re.IGNORECASE
)


@functools.lru_cache(maxsize=2048)
def _split_artist_names_cached(artist_str: Optional[str]) -> tuple:
    if not artist_str:
        return ()
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
    return tuple(sorted(result))


def split_artist_names(artist_str: Optional[str]) -> Set[str]:
    """Разбивает строку исполнителей на отдельные имена."""
    return set(_split_artist_names_cached(artist_str))


@functools.lru_cache(maxsize=4096)
def phonetic_artist_key(text: Optional[str]) -> str:
    """
    Фонетическая нормализация имени артиста для устойчивого сопоставления
    между латиницей и кириллицей (например Psychea <-> Психея, Tchaikovsky <-> Чайковский).
    """
    if not text:
        return ""
    s = transliterate_text(text).lower()
    s = re.sub(r'[\W_]+', ' ', s).strip()
    words = s.split()
    norm_words = []
    for w in words:
        if w in {"the", "a", "an"}:
            continue
        w = w.replace('tch', 'ch').replace('kh', 'h').replace('ch', 'h').replace('x', 'h')
        w = w.replace('ph', 'f').replace('w', 'v').replace('ck', 'k').replace('c', 'k')
        w = w.replace('ts', 'c').replace('tz', 'c')
        w = w.replace('ea', 'ya').replace('ey', 'y').replace('ia', 'ya')
        w = w.replace('y', 'i').replace('j', 'i')
        w = re.sub(r'(.)\1+', r'\1', w)
        if w:
            norm_words.append(w)
    return " ".join(norm_words)


_IN_WORD_HOMOGLYPH_I_RE = re.compile(r'(?<=[a-zA-Z])I(?=[a-zA-Z0-9])')
_IN_WORD_HOMOGLYPH_1_RE = re.compile(r'(?<=[a-zA-Z])1(?=[a-zA-Z])')


def _normalize_visual_homoglyphs(text: str) -> str:
    """
    Нормализует визуальные типографические омоглифы без ослабления строгого сравнения:
    заменяет заглавную 'I' на 'l' и leet '1' на 'l' исключительно внутри слова
    (например, 'RAIii' -> 'RAlii', 'bIink' -> 'blink', 'RA1ii' -> 'RAlii').
    Не затрагивает заглавные 'I' в начале слов (Ian, Igor, Imagine) и обычные строчные буквы.
    """
    if not text:
        return ""
    s = _IN_WORD_HOMOGLYPH_I_RE.sub('l', text)
    s = _IN_WORD_HOMOGLYPH_1_RE.sub('l', s)
    return s


def validate_artist_match(expected_artist: Optional[str], candidate_text: Optional[str]) -> bool:
    """
    Строгая валидация совпадения исполнителя:
    - проверяет точные нормализованные токены артиста в тексте кандидата (uploader/channel/title);
    - поддерживает двустороннюю транслитерацию и фонетическое сопоставление (Psychea <-> Психея);
    - поддерживает мульти-артистов, feat, ft, &, запятые;
    - предотвращает ложные совпадения подстрок ("Eve" != "Steve Aoki", "Ian" != "Ariana Grande", "A" != "The A").
    """
    if not expected_artist or not candidate_text:
        return True

    orig_expected = clean_unicode_text(expected_artist)
    orig_cand = clean_unicode_text(candidate_text)
    clean_expected = orig_expected.lower()
    clean_cand = orig_cand.lower()

    if clean_expected in clean_cand:
        pattern = r'(?<![a-z0-9а-яё])' + re.escape(clean_expected) + r'(?![a-z0-9а-яё])'
        if re.search(pattern, clean_cand):
            return True

    # Типографические омоглифы латиницы для стилизованных ников (например, DJ ZUP RAlii <-> _DJ ZUP RAIii_)
    expected_homo = _normalize_visual_homoglyphs(orig_expected).lower()
    cand_homo = _normalize_visual_homoglyphs(orig_cand).lower()
    if (expected_homo != clean_expected or cand_homo != clean_cand) and len(expected_homo) >= 4 and expected_homo in cand_homo:
        pattern_homo = r'(?<![a-z0-9а-яё])' + re.escape(expected_homo) + r'(?![a-z0-9а-яё])'
        if re.search(pattern_homo, cand_homo):
            return True

    cand_tr = transliterate_text(clean_cand)
    if clean_expected in cand_tr:
        pattern = r'(?<![a-z0-9])' + re.escape(clean_expected) + r'(?![a-z0-9])'
        if re.search(pattern, cand_tr):
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
            art_homo = _normalize_visual_homoglyphs(art).lower()
            if (art_homo != art_norm or cand_homo != clean_cand) and len(art_homo) >= 4 and art_homo in cand_homo:
                pattern_homo = r'(?<![a-z0-9а-яё])' + re.escape(art_homo) + r'(?![a-z0-9а-яё])'
                if re.search(pattern_homo, cand_homo):
                    return True
            art_tr = transliterate_text(art_norm)
            if art_tr != art_norm and len(art_tr) >= 2 and art_tr not in {"the", "a", "an"}:
                pattern_tr = r'(?<![a-z0-9а-яё])' + re.escape(art_tr) + r'(?![a-z0-9а-яё])'
                if re.search(pattern_tr, clean_cand):
                    return True
            if len(art_tr) >= 2:
                pattern_cand_tr = r'(?<![a-z0-9])' + re.escape(art_tr) + r'(?![a-z0-9])'
                if re.search(pattern_cand_tr, cand_tr):
                    return True

            art_pk = phonetic_artist_key(art_norm)
            if len(art_pk) >= 3:
                cand_pk = phonetic_artist_key(clean_cand)
                pattern_pk = r'(?<![a-z0-9])' + re.escape(art_pk) + r'(?![a-z0-9])'
                if re.search(pattern_pk, cand_pk):
                    return True

    return False


def count_matched_artists(expected_artist: Optional[str], candidate_text: Optional[str]) -> int:
    """
    Возвращает количество исполнителей из expected_artist, найденных в candidate_text.
    Позволяет отдавать приоритет кандидатам, содержащим ВСЕХ исполнителей совместного релиза.
    """
    if not expected_artist or not candidate_text:
        return 0
    sub_artists = split_artist_names(expected_artist)
    if not sub_artists:
        return 1 if validate_artist_match(expected_artist, candidate_text) else 0
    return sum(1 for a in sub_artists if validate_artist_match(a, candidate_text))


# -------------------------------------------------------------
# 7. Title Matching & Eponymous Protection
# -------------------------------------------------------------

@functools.lru_cache(maxsize=2048)
def _extract_core_title_words_cached(title: Optional[str], artist: Optional[str]) -> tuple:
    if not title:
        return ()
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
            return tuple(sorted(t for t in raw_tokens if len(t) >= 1))
        for aw in artist_tokens:
            if len(aw) >= 2:
                raw = re.sub(r'(?<!\w)' + re.escape(aw) + r'(?!\w)', ' ', raw)

    tokens = set(re.findall(r'[\w]+', raw))
    return tuple(sorted(t for t in tokens if len(t) >= 1))


def extract_core_title_words(title: Optional[str], artist: Optional[str] = None) -> Set[str]:
    """
    Извлекает ключевые слова названия трека с защитой от eponymous-запросов
    (The Drums - Drums, Bad Company - Bad Company, Future - Future).
    """
    return set(_extract_core_title_words_cached(title, artist))


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
    - Поддерживает специальный формат '--' (Исполнитель 1, Исполнитель 2... -- Название);
    - Поддерживает разделители: ' — ', ' - ', ' – ', '—', '–', ' : ', ' / ', ' | ';
    - Защищает составные имена исполнителей с дефисами (A-Ha, Jay-Z, Blink-182, T-Pain, AC/DC);
    - Если разделителя нет, возвращает (None, query).
    """
    cleaned = clean_unicode_text(query)
    if not cleaned:
        return None, ""

    # Приоритет специальному разделителю нескольких исполнителей '--'
    if "--" in cleaned:
        parts = re.split(r'\s*--\s*', cleaned, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()

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


def parse_multi_artist_query(query: str) -> Tuple[List[str], Optional[str], Optional[str]]:
    """
    Разбирает поисковый запрос с поддержкой одного или нескольких исполнителей.
    Форматы ввода:
      1. Специальный multi-artist формат:
         'Исполнитель 1, Исполнитель 2... -- Название'
      2. Стандартный формат с длинным тире:
         'Исполнитель 1, Исполнитель 2 — Название'
      3. Другие тире и разделители:
         'Исполнитель - Название', 'Исполнитель – Название'

    Возвращает:
      (artists_list, clean_title, raw_artist_string)
      где artists_list = [Исполнитель 1, Исполнитель 2, ...],
      clean_title = 'Название' (или None, если разделитель не найден),
      raw_artist_string = 'Исполнитель 1, Исполнитель 2' (или None).
    """
    if not query:
        return [], None, None

    cleaned = clean_unicode_text(query).strip()
    if not cleaned:
        return [], None, None

    artist_part, title_part = None, None

    # 1. Приоритет специальному разделителю '--' для нескольких исполнителей
    if "--" in cleaned:
        parts = re.split(r'\s*--\s*', cleaned, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            artist_part, title_part = parts[0].strip(), parts[1].strip()

    # 2. Стандартные разделители с длинным тире или пробелами вокруг
    if not artist_part:
        for sep in [" — ", " – ", " - ", "—", "–"]:
            if sep in cleaned:
                parts = cleaned.split(sep, 1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    artist_part, title_part = parts[0].strip(), parts[1].strip()
                    break

    # 3. Резервный разделитель 'by' ("Title by Artist")
    if not artist_part:
        by_match = re.split(r'\s+\bby\b\s+', cleaned, maxsplit=1, flags=re.IGNORECASE)
        if len(by_match) == 2 and by_match[0].strip() and by_match[1].strip():
            artist_part, title_part = by_match[1].strip(), by_match[0].strip()

    if not artist_part or not title_part:
        return [], None, None

    # Извлечение структурированного списка артистов (строго через запятую согласно синтаксису "Исполнитель 1, Исполнитель 2... -- Название")
    # Не разбиваем автоматически внутренние символы артиста (&, /, x, feat), чтобы не повреждать имена вроде AC/DC, Above & Beyond, Guns N' Roses.
    raw_artists = [a.strip() for a in artist_part.split(",") if a.strip()]
    if not raw_artists:
        raw_artists = [artist_part]

    # Сохраняем исходный порядок исполнителей, удаляя точные дубликаты
    seen = set()
    artists_list = []
    for a in raw_artists:
        low = a.lower()
        if low not in seen:
            seen.add(low)
            artists_list.append(a)

    return artists_list, title_part, ", ".join(artists_list)


def format_track_display(
    artists: Any,
    title: Optional[str] = None
) -> str:
    """
    Форматирует отображение трека СТРОГО по единому продуктовому стандарту:
      'Исполнитель — Название'
    или при нескольких исполнителях:
      'Исполнитель 1, Исполнитель 2, Исполнитель 3 — Название'

    Правила:
      - сначала список исполнителей через запятую с пробелом;
      - затем только длинное тире ' — ';
      - затем название трека;
      - порядок исполнителей сохраняется;
      - дубликаты исполнителей исключаются.
    """
    if not artists and not title:
        return "Неизвестный исполнитель — Неизвестный трек"

    # Если передан объект с display_name, artist, title (например ExtractedTrack, TrackIdentity, DownloadedAudio)
    if hasattr(artists, "artist") and hasattr(artists, "title") and title is None:
        title = getattr(artists, "title", None)
        artists = getattr(artists, "artist", None)

    # Нормализация списка исполнителей
    clean_artists = []
    seen = set()

    if isinstance(artists, (list, tuple, set)):
        items = list(artists)
    elif isinstance(artists, str):
        # Если строка уже содержит запятые (например "Drake, 21 Savage")
        if "," in artists:
            items = [a.strip() for a in artists.split(",") if a.strip()]
        else:
            items = [artists.strip()] if artists.strip() else []
    else:
        items = [str(artists).strip()] if artists else []

    for item in items:
        if not item:
            continue
        c_item = clean_unicode_text(str(item)).strip()
        if c_item and c_item.lower() not in seen:
            seen.add(c_item.lower())
            clean_artists.append(c_item)

    art_str = ", ".join(clean_artists) if clean_artists else "Неизвестный исполнитель"
    tit_str = clean_unicode_text(str(title)).strip() if title else "Неизвестный трек"

    return f"{art_str} — {tit_str}"


def is_artist_in_title_inversion(
    expected_artist: Optional[str],
    candidate_title: Optional[str],
    expected_title: Optional[str] = None,
    candidate_uploader: Optional[str] = None,
    candidate_channel: Optional[str] = None
) -> bool:
    """
    Проверяет, не является ли кандидат чужим треком другого артиста,
    где имя expected_artist ошибочно находится на позиции названия песни (Song Title),
    а не исполнителя (Artist):
    Например:
      expected_artist = "Tribal Church", expected_title = "Pt.02"
      candidate_title = "Joe Inferno - Tribal Church Feat Dye Witness"
      -> 'Joe Inferno' - реальный артист кандидата (не совпадает с Tribal Church и не совпадает с Pt.02).
      -> 'Tribal Church' - название песни чужого артиста (не совпадает с Pt.02).
      -> uploader/channel не принадлежат 'Tribal Church'.
      -> Это 100% инверсия подмены артиста (чужой трек)!
    """
    if not expected_artist or not candidate_title:
        return False

    # 1. Если uploader или channel явно принадлежат ожидаемому исполнителю,
    # это официальный канал/топик, никакой подмены нет.
    if candidate_uploader and validate_artist_match(expected_artist, candidate_uploader):
        return False
    if candidate_channel and validate_artist_match(expected_artist, candidate_channel):
        return False

    # 2. Ищем разделитель "Артист - Название" в названии кандидата
    cand_artist_part, cand_title_part = parse_query_artist_title(candidate_title)
    if not cand_artist_part or not cand_title_part:
        return False

    # Проверяем, совпадает ли левая часть (cand_artist_part) с expected_artist
    cand_artist_matches_expected = validate_artist_match(expected_artist, cand_artist_part)
    if cand_artist_matches_expected:
        return False

    # Проверяем, содержит ли правая часть (cand_title_part) имя expected_artist
    expected_artist_in_title_part = validate_artist_match(expected_artist, cand_title_part)
    if not expected_artist_in_title_part:
        return False

    # Здесь: expected_artist находится в правой части (в названии песни),
    # а в левой части (на позиции исполнителя) указан ДРУГОЙ исполнитель!
    # Исключение 1: обратный формат "Название - Исполнитель" (Reversed Query):
    # Если левая часть совпадает с expected_title, то это валидный обратный формат.
    if expected_title:
        # Для eponymous-треков (где название песни совпадает с именем артиста, например ABBA - ABBA):
        # Если левая часть cand_artist_part не совпадает с expected_artist, то кандидат вида "Some Other Artist - ABBA"
        # является 100% инверсией чужого артиста.
        if clean_unicode_text(expected_artist).lower() == clean_unicode_text(expected_title).lower():
            return True

        core_title = extract_core_title_words(expected_title, expected_artist)
        if core_title and compute_title_match_ratio(cand_artist_part, core_title) >= 0.5:
            return False  # Это "Title - Artist", не инверсия чужого артиста!

    return True


def validate_candidate_artist(
    expected_artist: Optional[str],
    candidate_title: Optional[str],
    candidate_uploader: Optional[str] = None,
    candidate_channel: Optional[str] = None,
    expected_title: Optional[str] = None,
    candidate_artist: Optional[str] = None
) -> bool:
    """
    Комплексная проверка соответствия исполнителя кандидата:
    - проверяет candidate_artist из метаданных трека (yt-dlp artist/creator);
    - проверяет uploader / channel (включая Topic, VEVO, официальные каналы);
    - проверяет позицию артиста в названии ("Artist - Title" или "Title - Artist");
    - отсекает случаи подмены артиста (is_artist_in_title_inversion);
    - поддерживает официальные каналы-агрегаторы дистрибуции (Release - Topic, Various Artists - Topic);
    - проверяет вхождения составных имен (мульти-артисты, feat, ft, &, транслит).
    """
    if not expected_artist:
        return True

    # 1. Если это инверсия (чужой трек другого артиста с названием = имени нашего артиста) -> False!
    if is_artist_in_title_inversion(
        expected_artist=expected_artist,
        candidate_title=candidate_title,
        expected_title=expected_title,
        candidate_uploader=candidate_uploader,
        candidate_channel=candidate_channel
    ):
        return False

    # 2. Проверяем метаданные артиста от yt-dlp (artist / creator), если доступны
    if candidate_artist and validate_artist_match(expected_artist, candidate_artist):
        return True

    # 3. Проверяем uploader / channel
    if candidate_uploader and validate_artist_match(expected_artist, candidate_uploader):
        return True
    if candidate_channel and validate_artist_match(expected_artist, candidate_channel):
        return True

    # 4. Проверяем название кандидата
    if candidate_title:
        cand_artist_part, cand_title_part = parse_query_artist_title(candidate_title)
        if cand_artist_part and cand_title_part:
            # Прямой формат: "Artist - Title"
            if validate_artist_match(expected_artist, cand_artist_part):
                return True
            # Обратный формат: "Title - Artist" (если левая часть похожа на title)
            if expected_title:
                core_title = extract_core_title_words(expected_title, expected_artist)
                if core_title and compute_title_match_ratio(cand_artist_part, core_title) >= 0.5:
                    if validate_artist_match(expected_artist, cand_title_part):
                        return True
        else:
            # Название без разделителя (например "Tribal Church Pt.02"):
            if validate_artist_match(expected_artist, candidate_title):
                return True

    # 5. Проверяем официальные каналы-агрегаторы релизов YouTube Music (Release - Topic, Various Artists - Topic)
    uploader_clean = clean_unicode_text(candidate_uploader or "").lower().strip()
    channel_clean = clean_unicode_text(candidate_channel or "").lower().strip()
    is_universal_release_topic = (
        uploader_clean in {"release - topic", "various artists - topic", "release", "various artists"} or
        channel_clean in {"release - topic", "various artists - topic", "release", "various artists"}
    )
    if is_universal_release_topic and expected_title and candidate_title:
        core_title = extract_core_title_words(expected_title, expected_artist)
        if core_title and compute_title_match_ratio(candidate_title, core_title) >= 0.5:
            return True

    return False
