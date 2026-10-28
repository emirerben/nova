"""Two hand-written model responses for the Cunda fixture, used to prove the checks separate.

Synthetic: these are NOT recorded model output and say nothing about what the agent does.
`DIARY` imitates the failure being fixed (recap voice, generic mood). `SOCIAL` is the target
register: a viewer-directed claim per idea, the place and concrete features named, nothing
beyond what the fixture's descriptions and event context support. Moment refs are the
request-local m1..m12 in `camera_roll_voice_fixture.candidates()` order.
"""

import json


def _idea(title, moments, form, audience, angle, why, opening, closing, caption):
    return {
        "title": title,
        "moment_ids": moments,
        "research_fact_ids": [],
        "content_plan": {
            "form": form,
            "audience": audience,
            "angle": angle,
            "why_it_works": why,
            "opening_text": opening,
            "closing_text": closing,
            "caption": caption,
            "beat_reasons": ["Shows this part of the outing"] * len(moments),
        },
    }


DIARY = {
    "stories": [
        _idea(
            "A slow day wandering the island",
            ["m1", "m2", "m4", "m9"],
            "atmosphere",
            "Friends and followers",
            "A relaxed day in the life, wandering old streets and the harbour.",
            "A calm mood piece from the day.",
            "My day wandering the old streets",
            None,
            "Spent the day exploring stone streets and the harbour. Such a peaceful day.",
        ),
        _idea(
            "Sunset to end the day",
            ["m5", "m6", "m7"],
            "atmosphere",
            "Followers who like nature",
            "We ended the day by the sea as the light went orange.",
            "Golden light makes a pleasant ending.",
            "We ended the day by the water",
            "Today was perfect",
            "Today was so perfect. Golden hour by the sea. #memories",
        ),
        _idea(
            "Lunch and a new friend",
            ["m11", "m12"],
            "review",
            "Followers",
            "I met a cat and had lunch by the water.",
            "A cosy personal moment.",
            "I met a cat and had lunch by the harbour",
            None,
            "Today I ate grilled fish and met the sweetest cat. Beautiful day.",
        ),
    ]
}

SOCIAL = {
    "stories": [
        _idea(
            "Why Cunda is worth a day trip",
            ["m1", "m3", "m4", "m7"],
            "guide",
            "People planning an Aegean trip",
            "Cunda's stone streets, windmill and olive grove make the island worth visiting.",
            "A stranger sees four concrete reasons to visit in a few seconds.",
            "Skip the resort. Walk Cunda's stone streets instead.",
            "A windmill, old houses and olive trees",
            "Stone houses, a windmill and an olive grove on Cunda Island. Save this for your trip.",
        ),
        _idea(
            "Why people stay for sunset in Cunda",
            ["m6", "m9", "m10"],
            "atmosphere",
            "Travellers choosing where to stay",
            "Cunda's harbour and sea at the end of the day, shown as a reason to stay for it.",
            "The harbour and sunset give a plan the viewer can copy.",
            "This is why people stay for sunset in Cunda",
            None,
            "Fishing boats, calm water and an orange horizon. Worth planning your evening around.",
        ),
        _idea(
            "Cunda's cats own the stone steps",
            ["m11", "m12"],
            "review",
            "Anyone who likes small-town food and animals",
            "A cat on a Cunda doorstep, then grilled fish and meze by the water.",
            "A small, specific scene with a question to answer in the comments.",
            "The best welcome in Cunda is a cat on the steps",
            "Then grilled fish by the water",
            "Meze by the water, and the local cats. Which would you try first?",
        ),
    ]
}


def raw(sample: dict) -> str:
    return json.dumps(sample)
