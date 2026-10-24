"""KRI-838: the deterministic text language-ID behind the caption-language verdict.

Failure modes this guards (written before the scorer, from the real incident texts):

 1. Caro's English caption, full of Spanish dish names ("Oblaya de la Casa", "dulce de
    leche", "leche condensada"), must read as English, never Spanish.
 2. whisper forced to Turkish on English speech writes the English again; that text must
    CONTRADICT a Turkish request instead of being believed because of its label.
 3. Lucas's Spanish (whisper forced to es) must be confirmed, with no translation needed.
 4. whisper's Finnish-looking hallucination (auto-detect on a noisy, mixed clip) is in no
    table language: "unknown", never a guess.
 5. Short captions ("Cheers!", "Hello") are "unknown" — a failure needs positive evidence.
 6. Spanish / Portuguese / Italian share many function words; real sentences of each must
    not be confused with one another.
 7. Capitalized mid-sentence words and the creator's own terms are names, not evidence.
 8. Turkish text with Spanish dish names stays Turkish.
 9. An all-caps caption is read, not skipped.
10. Arabic is recognised by script.

The non-English/Turkish sentences below are authored test input (not model output).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.caption_language import (
    caption_language_verdict,
    detect_text_language,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kri838_caption_language"


def _fixture_text(name: str) -> str:
    words = json.loads((FIXTURES / name).read_text(encoding="utf-8"))["words"]
    return " ".join(word["text"] for word in words)


# --- real whisper output (local whisper.cpp medium on the incident clips) -------------

CARO_AUTO = _fixture_text("caro_t2_auto_en_words.json")
CARO_FORCED_TR = _fixture_text("caro_t2_forced_tr_words.json")
LUCAS_FORCED_ES = _fixture_text("lucas_t3_forced_es_words.json")
LUCAS_AUTO_FI = _fixture_text("lucas_t3_auto_words.json")
CARO_GLOSSARY = ("Oblea de la Casa", "dulce de leche", "leche condensada")


def test_the_real_fixtures_say_what_the_incident_logs_say():
    # Guards the fixtures themselves: a regenerated file must keep the bug's shape.
    assert CARO_AUTO.startswith("Hi, I'm Carolina from Ecuador")
    assert CARO_FORCED_TR.startswith("Hi, I'm Carolina from Ecuador")  # "Turkish": still English
    assert "Acá tenemos salsas tradicionales argentinas" in LUCAS_FORCED_ES
    assert LUCAS_AUTO_FI.startswith("Tupana on ispanjollinen")


def test_caro_english_with_spanish_dish_names_is_english_not_spanish():
    assert detect_text_language(CARO_AUTO) == "en"
    assert detect_text_language(CARO_AUTO, ignore_terms=CARO_GLOSSARY) == "en"


def test_whisper_forced_turkish_on_english_speech_contradicts_a_turkish_request():
    assert caption_language_verdict([CARO_FORCED_TR], "tr") == ("contradicted", "en")
    assert caption_language_verdict([CARO_FORCED_TR], "tr", ignore_terms=CARO_GLOSSARY) == (
        "contradicted",
        "en",
    )
    assert caption_language_verdict([CARO_FORCED_TR], "en") == ("confirmed", "en")


def test_lucas_spanish_is_confirmed_for_a_spanish_request():
    assert detect_text_language(LUCAS_FORCED_ES) == "es"
    assert caption_language_verdict([LUCAS_FORCED_ES], "es") == ("confirmed", "es")
    assert caption_language_verdict([LUCAS_FORCED_ES], "tr") == ("contradicted", "es")


def test_a_hallucination_in_no_table_language_is_unknown():
    assert detect_text_language(LUCAS_AUTO_FI) is None
    assert caption_language_verdict([LUCAS_AUTO_FI], "es") == ("unknown", None)


@pytest.mark.parametrize(
    "text", ["Cheers!", "Hello", "Thanks", "OK", "", "   ", "Don Julio", "123 456"]
)
def test_short_or_empty_text_is_unknown(text):
    assert detect_text_language(text) is None
    assert caption_language_verdict([text], "tr")[0] == "unknown"


def test_a_request_outside_the_table_is_unknown_not_contradicted():
    assert caption_language_verdict(["Hello there, this is plain English text."], "ru") == (
        "unknown",
        "en",
    )
    assert (
        caption_language_verdict(["Hello there, this is plain English text."], None)[0] == "unknown"
    )


# --- authored sentences, one block per language ---------------------------------------

AUTHORED = {
    "en": [
        "Hello there, this is plain English text.",
        "Hi, I'm Carolina from Ecuador.",
        "We went to the market and bought some fresh bread for the weekend.",
        "I think that you should call them before they close the shop.",
        "That's it. Thank you so much for watching.",
        "What are you going to do with all of this?",
    ],
    "tr": [
        "Bugün çok güzel bir gün, hadi dışarı çıkalım.",
        "Merhaba, ben Ecem ve bu benim yeni videom.",
        "Bu tatlı çok lezzetli, bence herkes denemeli.",
        "Ekvadorlu Carolina'dan geleneksel Kolombiya tatlısı.",
        "Tamam, bu kadar.",
        "Yarın sabah erkenden yola çıkacağız ve akşama kadar orada olacağız.",
    ],
    "es": [
        "Hola, buenos días. Hoy vamos a preparar una receta muy sencilla para toda la familia.",
        "¿Cómo estás? Estoy muy bien, gracias.",
        "Acá tenemos salsas tradicionales argentinas.",
        "No sé qué hacer con esto, pero me gusta mucho.",
        "Es la mejor pizza de la ciudad y todos lo saben.",
        "Nosotros siempre venimos aquí con mis amigos los domingos.",
    ],
    "pt": [
        "Olá a todos, hoje vamos preparar uma receita muito simples para a família.",
        "Eu não sei o que fazer com isso, mas é muito bonito.",
        "Nós sempre viemos aqui com os meus amigos aos domingos.",
        "Obrigado por assistir, até a próxima.",
        "Você já provou o pão de queijo da minha avó?",
    ],
    "it": [
        "Ciao a tutti, oggi prepariamo una ricetta molto semplice per la famiglia.",
        "Non so cosa fare con questo, ma è bellissimo.",
        "Andiamo al mercato e compriamo il pane per la cena di stasera.",
        "Grazie mille, ci vediamo domani con una nuova ricetta.",
        "Questo è il posto dove mangiamo sempre con gli amici.",
    ],
    "fr": [
        "Bonjour à tous, aujourd'hui nous allons faire une recette très simple.",
        "Je ne sais pas ce que tu veux faire avec ça.",
        "C'est le meilleur restaurant de la ville et tout le monde le sait.",
        "Merci beaucoup d'avoir regardé, à bientôt dans une nouvelle vidéo.",
        "Nous sommes allés au marché pour acheter du pain et des fruits.",
    ],
    "de": [
        "Guten Morgen, ich habe heute keine Zeit für einen langen Spaziergang.",
        "Das ist wirklich ein sehr schönes Haus.",
        "Wir gehen nach dem Essen zusammen in den Park und trinken einen Kaffee.",
        "Vielen Dank fürs Zuschauen, bis zum nächsten Mal.",
    ],
    "nl": [
        "Hallo allemaal, vandaag maken we een heel simpel recept voor het hele gezin.",
        "Ik weet niet wat ik hiermee moet doen, maar het is heel mooi.",
        "Wij gaan na het eten samen naar het park en drinken een kopje koffie.",
        "Bedankt voor het kijken, tot de volgende keer.",
    ],
    "ar": [
        "مرحبا بكم جميعا في هذا الفيديو الجديد",
        "شكرا جزيلا على المشاهدة",
    ],
}


@pytest.mark.parametrize(
    ("expected", "text"),
    [(code, text) for code, texts in AUTHORED.items() for text in texts],
)
def test_authored_sentences_are_identified(expected, text):
    assert detect_text_language(text) == expected


def test_no_authored_sentence_is_identified_as_another_language():
    wrong = [
        (expected, text, detect_text_language(text))
        for expected, texts in AUTHORED.items()
        for text in texts
        if detect_text_language(text) not in (expected, None)
    ]
    assert wrong == []


# --- names, the creator's own terms, capitalization ----------------------------------


def test_turkish_translation_of_caro_with_spanish_dish_names_stays_turkish():
    # Authored stub of a Turkish rendering of Caro's lines (not a recorded model output).
    lines = [
        "Merhaba, ben Ekvador'dan Carolina ve bu Oblea de la Casa.",
        "Geleneksel bir Kolombiya tatlısı.",
        "Gofret bisküvi, ekmek, hayır, böğürtlen reçeli, peynir, dulce de leche, "
        "leche condensada ve krem şanti ile yapılıyor.",
        "Çok güzel, lezzetli. Hepsi bu kadar.",
    ]
    assert caption_language_verdict(lines, "tr") == ("confirmed", "tr")
    assert caption_language_verdict(lines, "tr", ignore_terms=CARO_GLOSSARY) == ("confirmed", "tr")
    assert caption_language_verdict(lines, "en") == ("contradicted", "tr")


def test_the_creators_own_terms_carry_no_evidence():
    only_names = "Oblea de la Casa, dulce de leche, leche condensada."
    assert detect_text_language(only_names, ignore_terms=CARO_GLOSSARY) is None


def test_a_name_in_the_middle_of_an_english_sentence_is_not_evidence():
    assert (
        detect_text_language("Today we are visiting Beşiktaş and Kadıköy with my friends.") == "en"
    )


def test_all_caps_captions_are_read_not_skipped():
    assert detect_text_language("THIS IS THE BEST PLACE IN THE WHOLE CITY") == "en"
    assert detect_text_language("BU ÇOK GÜZEL BİR GÜN VE BEN ÇOK MUTLUYUM") == "tr"


def test_a_mixed_caption_set_follows_its_majority():
    lines = [
        "Hi, I'm Carolina from Ecuador.",
        "This is the traditional dessert we make at home.",
        "Gracias.",
        "It's really good, you should try it with your friends.",
    ]
    assert caption_language_verdict(lines, "en") == ("confirmed", "en")


def test_cues_are_read_as_one_text():
    # Each cue alone is too short to say; together they are plainly Turkish.
    lines = ["Bu çok", "güzel bir", "gün ve ben", "çok mutluyum."]
    assert caption_language_verdict(lines, "tr") == ("confirmed", "tr")


# --- held out: ten new sentences per language, written after the word lists were tuned --

HELD_OUT = {
    "en": [
        "Welcome back to the channel, today I'm showing you how I organise my kitchen.",
        "So this is the place where we had dinner last night, and it was amazing.",
        "Don't forget to like and subscribe if you enjoyed the video.",
        "She said that they would be late, but nobody believed her.",
        "Look at that view, it's unreal.",
        "First you take the dough, then you roll it out and cut it into squares.",
        "Where did you buy this? I've been looking for one everywhere.",
        "My brother and his wife are coming over for the weekend.",
        "It was raining the whole day, so we stayed inside and played cards.",
        "Let's go, we are going to be late for the train.",
    ],
    "tr": [
        "Kanala hoş geldiniz, bugün size mutfağımı nasıl düzenlediğimi göstereceğim.",
        "Dün akşam yemek yediğimiz yer burası ve gerçekten harikaydı.",
        "Videoyu beğendiyseniz beğenmeyi ve abone olmayı unutmayın.",
        "Önce hamuru alıyorsun, sonra açıp kare kare kesiyorsun.",
        "Bunu nereden aldın? Her yerde arıyordum.",
        "Kardeşim ve eşi hafta sonu bize geliyor.",
        "Bütün gün yağmur yağdı, o yüzden içeride kalıp kağıt oynadık.",
        "Hadi gidelim, trene geç kalacağız.",
        "Manzaraya bak, inanılmaz.",
        "Ben bunu çok sevdim ama biraz pahalı.",
    ],
    "es": [
        "Bienvenidos de nuevo al canal, hoy les muestro cómo organizo mi cocina.",
        "Este es el lugar donde cenamos anoche y fue increíble.",
        "No se olviden de darle me gusta y suscribirse si les gustó el video.",
        "Primero tomas la masa, luego la estiras y la cortas en cuadrados.",
        "¿Dónde compraste esto? Lo estuve buscando por todas partes.",
        "Mi hermano y su esposa vienen a casa el fin de semana.",
        "Estuvo lloviendo todo el día, así que nos quedamos adentro jugando a las cartas.",
        "Vamos, que vamos a llegar tarde al tren.",
        "Mirá esa vista, es increíble.",
        "A mí me encanta, pero es un poco caro.",
    ],
    "pt": [
        "Bem-vindos de volta ao canal, hoje eu vou mostrar como organizo a minha cozinha.",
        "Este é o lugar onde jantamos ontem à noite e foi incrível.",
        "Não se esqueçam de curtir e se inscrever se vocês gostaram do vídeo.",
        "Primeiro você pega a massa, depois estica e corta em quadrados.",
        "Onde você comprou isso? Eu estava procurando em todo lugar.",
        "O meu irmão e a mulher dele vêm cá no fim de semana.",
        "Choveu o dia inteiro, então ficamos dentro de casa jogando cartas.",
        "Vamos, que a gente vai chegar atrasado para o trem.",
        "Olha só essa vista, é inacreditável.",
        "Eu adorei, mas é um pouco caro.",
    ],
    "it": [
        "Bentornati sul canale, oggi vi mostro come organizzo la mia cucina.",
        "Questo è il posto dove abbiamo cenato ieri sera ed è stato fantastico.",
        "Non dimenticate di mettere mi piace e iscrivervi se il video vi è piaciuto.",
        "Prima prendi l'impasto, poi lo stendi e lo tagli a quadrati.",
        "Dove l'hai comprato? Lo cercavo dappertutto.",
        "Mio fratello e sua moglie vengono a trovarci nel fine settimana.",
        "Ha piovuto tutto il giorno, quindi siamo rimasti dentro a giocare a carte.",
        "Andiamo, che faremo tardi per il treno.",
        "Guarda che vista, è incredibile.",
        "A me piace molto, ma è un po' caro.",
    ],
    "fr": [
        "Bienvenue sur la chaîne, aujourd'hui je vous montre comment j'organise ma cuisine.",
        "C'est l'endroit où nous avons dîné hier soir et c'était incroyable.",
        "N'oubliez pas de liker et de vous abonner si vous avez aimé la vidéo.",
        "D'abord tu prends la pâte, ensuite tu l'étales et tu la coupes en carrés.",
        "Où as-tu acheté ça ? Je le cherchais partout.",
        "Mon frère et sa femme viennent chez nous pour le week-end.",
        "Il a plu toute la journée, alors nous sommes restés à l'intérieur pour jouer aux cartes.",
        "Allons-y, nous allons être en retard pour le train.",
        "Regarde cette vue, c'est incroyable.",
        "Moi, j'adore ça, mais c'est un peu cher.",
    ],
    "de": [
        "Willkommen zurück auf dem Kanal, heute zeige ich euch, wie ich meine Küche organisiere.",
        "Das ist der Ort, an dem wir gestern Abend gegessen haben, und es war großartig.",
        "Vergesst nicht zu liken und zu abonnieren, wenn euch das Video gefallen hat.",
        "Zuerst nimmst du den Teig, dann rollst du ihn aus und schneidest ihn in Quadrate.",
        "Wo hast du das gekauft? Ich habe überall danach gesucht.",
        "Mein Bruder und seine Frau kommen am Wochenende zu Besuch.",
        "Es hat den ganzen Tag geregnet, deshalb sind wir drinnen geblieben und haben "
        "Karten gespielt.",
        "Komm, wir sind sonst zu spät für den Zug.",
        "Schau dir diese Aussicht an, das ist unglaublich.",
        "Ich finde es toll, aber es ist ein bisschen teuer.",
    ],
    "nl": [
        "Welkom terug op het kanaal, vandaag laat ik zien hoe ik mijn keuken organiseer.",
        "Dit is de plek waar we gisteravond hebben gegeten en het was geweldig.",
        "Vergeet niet te liken en je te abonneren als je de video leuk vond.",
        "Eerst pak je het deeg, dan rol je het uit en snij je het in vierkantjes.",
        "Waar heb je dit gekocht? Ik zocht er overal naar.",
        "Mijn broer en zijn vrouw komen dit weekend bij ons langs.",
        "Het regende de hele dag, dus we bleven binnen en speelden kaart.",
        "Kom op, we gaan te laat komen voor de trein.",
        "Kijk eens naar dat uitzicht, het is ongelooflijk.",
        "Ik vind het heel mooi, maar het is wel een beetje duur.",
    ],
}


def test_held_out_sentences_are_never_called_the_wrong_language():
    """Measured, not tuned for: wrong calls must stay at zero (unknown is allowed), and
    most sentences must still be identified (74 of 80 when this was written)."""
    wrong: list[tuple[str, str | None, str]] = []
    identified = 0
    total = 0
    for expected, sentences in HELD_OUT.items():
        for sentence in sentences:
            total += 1
            got = detect_text_language(sentence)
            if got == expected:
                identified += 1
            elif got is not None:
                wrong.append((expected, got, sentence))
    assert wrong == []
    assert identified >= int(total * 0.9)


# --- a name with a diacritic is a name, even as the first word (KRI-838 review) ----------

NAMES = [
    "Ayşe",
    "Şükrü",
    "Işıl",
    "Çağla",
    "Gözde",
    "İlker",
    "Doğan",
    "Ñusta",
    "Muñoz",
    "Núñez",
    "João",
    "Conceição",
    "Zoë",
]


@pytest.mark.parametrize("name", NAMES)
def test_a_sentence_initial_name_never_outvotes_the_english_around_it(name):
    one_line = f"{name} says hi to everyone."
    assert detect_text_language(one_line) in (None, "en")
    assert caption_language_verdict([one_line], "en")[0] != "contradicted"
    longer = f"{name} says hi to everyone and the whole team."
    assert detect_text_language(longer) == "en"


def test_a_sentence_initial_function_word_still_carries_its_characters():
    # "Şimdi" and "Teşekkürler" are Turkish function words, not names: their ş still counts.
    assert detect_text_language("Şimdi gidiyoruz.") == "tr"
    assert detect_text_language("Teşekkürler.") == "tr"
    assert detect_text_language("Ekvadorlu Carolina'dan geleneksel Kolombiya tatlısı.") == "tr"
