"""Segmentation: sentence boundaries, word tokens, offsets and line numbers."""

from meridian.prose import Locator, split_sentences
from meridian.prose.segment import STOPWORDS


def test_sentences_split_on_terminators_and_offsets_slice_back_exactly():
    text = "First sentence. Second one! Third?"
    sentences = split_sentences(text)
    assert [s.text for s in sentences] == ["First sentence.", "Second one!", "Third?"]
    for sentence in sentences:
        assert text[sentence.start : sentence.end] == sentence.text


def test_decimal_point_does_not_end_a_sentence():
    sentences = split_sentences("The value is 3.5 today. Next.")
    assert [s.text for s in sentences] == ["The value is 3.5 today.", "Next."]


def test_blank_line_ends_a_sentence_and_a_single_newline_does_not():
    text = "Line one\ncontinues here.\n\nNew paragraph."
    sentences = split_sentences(text)
    assert [s.text for s in sentences] == ["Line one\ncontinues here.", "New paragraph."]
    assert sentences[1].line == 4  # line 3 is the blank line


def test_closing_quote_after_a_terminator_ends_the_sentence():
    text = 'He said "stop." Then he left.'
    assert [s.text for s in split_sentences(text)] == ['He said "stop."', "Then he left."]


def test_words_are_lower_cased_with_straight_apostrophes_and_keep_offsets():
    text = "It’s Meridian’s launch."
    (sentence,) = split_sentences(text)
    assert [w.text for w in sentence.words] == ["it's", "meridian's", "launch"]
    second = sentence.words[1]
    assert text[second.start : second.end] == "Meridian’s"


def test_empty_and_punctuation_only_text_has_no_sentences():
    assert split_sentences("") == ()
    assert split_sentences("... --- !!!") == ()


def test_word_flags_mark_specific_content_words():
    (sentence,) = split_sentences("Postgres stores 1200 rows in Berlin.")
    assert [w.is_specific for w in sentence.words] == [False, False, True, False, False, True]


def test_stopwords_are_not_content_and_quantity_words_are_content():
    assert {"it", "is", "what"} <= STOPWORDS
    assert "few" not in STOPWORDS and "most" not in STOPWORDS
    (sentence,) = split_sentences("It is what it is.")
    assert not any(w.is_content for w in sentence.words)


def test_locator_maps_offsets_to_line_and_sentence_number():
    text = "One. Two\nthree.\n\nFour."
    locator = Locator(text, split_sentences(text))
    start = text.index("Four")
    location = locator.at(start, start + 4)
    assert (location.line, location.sentence) == (4, 3)


def test_locator_gives_sentence_zero_before_the_first_sentence():
    text = "... Hello."
    locator = Locator(text, split_sentences(text))
    assert locator.at(0, 3).sentence == 0
