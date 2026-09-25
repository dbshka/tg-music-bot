import pytest
from services.identity import (
    parse_multi_artist_query,
    format_track_display,
    count_matched_artists,
    split_artist_names,
)
from services.extractor import UNSUPPORTED_URL_FALLBACK_TEXT
from services.search import (
    _score_inline_candidate,
    extract_artist_title_from_query,
    SearchItem,
)
from handlers.tag_editor import get_menu_keyboard, get_audio_edit_keyboard, get_back_keyboard


class TestMultiArtistParsing:
    def test_single_artist_double_hyphen(self):
        artists, title, raw = parse_multi_artist_query("Queen -- Bohemian Rhapsody")
        assert artists == ["Queen"]
        assert title == "Bohemian Rhapsody"
        assert raw == "Queen"

    def test_two_artists_comma_double_hyphen(self):
        artists, title, raw = parse_multi_artist_query("Eminem, Rihanna -- The Monster")
        assert artists == ["Eminem", "Rihanna"]
        assert title == "The Monster"
        assert raw == "Eminem, Rihanna"

    def test_three_artists_comma(self):
        artists, title, raw = parse_multi_artist_query("David Guetta, Bebe Rexha, J Balvin -- Say My Name")
        assert artists == ["David Guetta", "Bebe Rexha", "J Balvin"]
        assert title == "Say My Name"

    def test_dash_variations(self):
        # em-dash
        artists1, title1, _ = parse_multi_artist_query("Travis Scott, Drake — Sicko Mode")
        assert artists1 == ["Travis Scott", "Drake"]
        assert title1 == "Sicko Mode"

        # en-dash
        artists2, title2, _ = parse_multi_artist_query("Calvin Harris, Dua Lipa – One Kiss")
        assert artists2 == ["Calvin Harris", "Dua Lipa"]
        assert title2 == "One Kiss"

        # single hyphen
        artists3, title3, _ = parse_multi_artist_query("Skrillex, Fred again.. - Rumble")
        assert artists3 == ["Skrillex", "Fred again.."]
        assert title3 == "Rumble"

    def test_internal_symbols_in_artist_and_title_preserved(self):
        # Slash inside artist name is preserved
        artists1, title1, _ = parse_multi_artist_query("AC/DC -- Thunderstruck")
        assert artists1 == ["AC/DC"]
        assert title1 == "Thunderstruck"

        # Ampersand inside artist and title is preserved
        artists2, title2, _ = parse_multi_artist_query("Above & Beyond -- Sun & Moon")
        assert artists2 == ["Above & Beyond"]
        assert title2 == "Sun & Moon"

        # Multiple artists with internal symbols separated by comma
        artists3, title3, _ = parse_multi_artist_query("AC/DC, Guns N' Roses -- Rock Medley")
        assert artists3 == ["AC/DC", "Guns N' Roses"]
        assert title3 == "Rock Medley"

    def test_deduplication_and_order(self):
        artists, title, _ = parse_multi_artist_query("Eminem, eminem, Rihanna -- Rap God")
        assert artists == ["Eminem", "Rihanna"]
        assert title == "Rap God"

    def test_no_separator_fallback(self):
        artists, title, raw = parse_multi_artist_query("Queen Bohemian Rhapsody")
        assert artists == []
        assert title is None
        assert raw is None


class TestTrackDisplayFormatting:
    def test_em_dash_used(self):
        disp = format_track_display("Queen", "Bohemian Rhapsody")
        assert disp == "Queen — Bohemian Rhapsody"
        assert "—" in disp  # em-dash \u2014
        assert " - " not in disp
        assert " – " not in disp

    def test_multiple_artists_list(self):
        disp = format_track_display(["Eminem", "Rihanna"], "The Monster")
        assert disp == "Eminem, Rihanna — The Monster"

    def test_multiple_artists_string(self):
        disp = format_track_display("Eminem, Rihanna", "The Monster")
        assert disp == "Eminem, Rihanna — The Monster"

    def test_feat_in_artist_string(self):
        disp = format_track_display("Eminem feat. Rihanna", "Love The Way You Lie")
        assert disp == "Eminem feat. Rihanna — Love The Way You Lie"
        assert "—" in disp

        disp2 = format_track_display(["Eminem", "Rihanna"], "Love The Way You Lie")
        assert disp2 == "Eminem, Rihanna — Love The Way You Lie"

    def test_deduplication(self):
        disp = format_track_display(["Eminem", "Eminem"], "Without Me")
        assert disp == "Eminem — Without Me"

    def test_missing_fields_fallbacks(self):
        assert format_track_display("", "Song Title") == "Неизвестный исполнитель — Song Title"
        assert format_track_display("Artist Name", "") == "Artist Name — Неизвестный трек"
        assert format_track_display("", "") == "Неизвестный исполнитель — Неизвестный трек"


class TestArtistMatchingAndRanking:
    def test_single_artist_matches_multi_artist_track(self):
        count1 = count_matched_artists("Eminem", "Eminem & Rihanna - The Monster")
        assert count1 == 1

        count2 = count_matched_artists("Rihanna", "Eminem & Rihanna - The Monster")
        assert count2 == 1

        count_none = count_matched_artists("Drake", "Eminem & Rihanna - The Monster")
        assert count_none == 0

    def test_all_artists_match(self):
        count_both = count_matched_artists("Eminem, Rihanna", "Eminem & Rihanna - The Monster")
        assert count_both == 2

    def test_extract_artist_title_uses_multi_artist(self):
        art, tit = extract_artist_title_from_query("Eminem, Rihanna -- The Monster")
        assert "Eminem" in art
        assert "Rihanna" in art
        assert tit == "The Monster"

    def test_candidate_scoring_multi_artist(self):
        item = SearchItem(
            index=1,
            title="The Monster",
            artist="Eminem, Rihanna",
            url="https://youtube.com/watch?v=123",
            duration=250
        )
        score_single = _score_inline_candidate(
            item=item,
            query_artist="Eminem",
            query_title="The Monster",
            requested_modifiers=set()
        )
        assert score_single > 1000.0

        score_full = _score_inline_candidate(
            item=item,
            query_artist="Eminem, Rihanna",
            query_title="The Monster",
            requested_modifiers=set()
        )
        assert score_full > score_single


class TestUXCopywritingAndIntegrity:
    def test_unsupported_url_fallback_text(self):
        text = UNSUPPORTED_URL_FALLBACK_TEXT
        assert "Spotify" in text
        assert "Apple Music" in text
        assert "YouTube" in text
        assert "SoundCloud" in text
        assert "Яндекс Музыка" in text
        assert "Возникла ошибка 24" in text
        assert "Исполнитель — Название" in text
        assert "—" in text  # em-dash

    def test_tag_editor_button_labels_and_callbacks(self):
        kb = get_menu_keyboard()
        buttons_flat = [btn for row in kb.inline_keyboard for btn in row]
        button_map = {btn.callback_data: btn.text for btn in buttons_flat}

        # Check concise, unified terminology
        assert button_map.get("tag:edit:artist") == "Исполнитель"
        assert button_map.get("tag:edit:title") == "Название"
        assert button_map.get("tag:edit:album") == "Альбом"
        assert button_map.get("tag:edit:cover") == "Обложка"
        assert button_map.get("tag:save") == "Сохранить"
        assert button_map.get("tag:cancel") == "Отмена"

        back_kb = get_back_keyboard()
        assert back_kb.inline_keyboard[0][0].text == "Назад"
        assert back_kb.inline_keyboard[0][0].callback_data == "tag:back"

        edit_kb = get_audio_edit_keyboard(123)
        assert edit_kb.inline_keyboard[0][0].text == "Изменить теги"
        assert edit_kb.inline_keyboard[0][0].callback_data == "audio:edit:123"

        # Check NO legacy confusing labels
        for text in button_map.values():
            assert "Артист" not in text
            assert "Автор" not in text
            assert "Применить и отправить" not in text
            assert "Назад в меню" not in text
