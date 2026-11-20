# Example `activity_context_prompt`

Frigate's default context prompt is written for a generic suburban house: porches, sidewalks,
deliveries between 6 AM and 10 PM. A short prompt about your own property works better. The
example below assumes the shim adds the profile and sun lines, and that your Frigate profiles are
named `home`, `away` and `night`. Change the names, the car and the routine to match your setup.

Put it under `review.genai.activity_context_prompt` globally. Cameras that set
`review.genai.enabled: true` inherit it.

```yaml
review:
  genai:
    enabled: false
    alerts: true
    detections: false
    preferred_language: English
    additional_concerns:
      - "False detection: no real person, car or motorcycle is visible - only leaves, shadow, rain, insect, spider web, reflection or headlight glare"
    activity_context_prompt: |
      ### Property
      Single-family home. The outdoor cameras normally cover only the private property
      (driveway, entrance, garden, terrace); public areas are masked out, so a person or vehicle
      in frame is usually on the property. If the frames clearly show a public road or path
      instead, people and vehicles that only pass by there without entering the property are
      Level 0 - this overrides the presence rules below (also for away and night).
      Residents: a couple. Their car: a dark blue station wagon. There is no face or plate
      recognition - call a matching car "probably the residents' car", never verified.

      ### Presence - use "Active Frigate profile" in the Sequence Details
      - away: nobody is home. Any person other than an obvious delivery, and any vehicle other
        than the residents' car, is Level 1. The residents' car arriving while away is most
        likely the residents coming back (Level 0, but say so).
      - home: the residents are home and awake; normal household activity.
      - night: the residents are home and asleep; any unknown person or vehicle is Level 1.
      - unknown/none: residents are usually home in the evening and at weekends.

      ### Daylight - use sunrise/sunset in the Sequence Details
      Compare "Activity started at" with today's sunrise and sunset. "After dark" means after
      sunset or before sunrise. Do not judge daylight from image brightness (infrared and
      headlights are misleading).

      ### Normal Activity (Level 0)
      - The residents' car arriving, leaving, parking; people getting in or out of it
      - Parcel and mail deliveries by any carrier, any time in daylight: walk to the entrance,
        drop, leave
      - With profile home: people gardening, on the terrace, carrying things between house,
        garage and car
      - Wildlife and pets at any time

      ### Suspicious Activity (Level 1)
      - Unknown person or vehicle while the profile is away or night (deliveries excepted)
      - Unknown person or vehicle on the property after dark
      - Anyone checking doors, windows, the garage or the car; peering inside; walking the
        perimeter; trying handles
      - Carrying items away from the terrace, garage or entrance (other than a delivery driver
        returning to the vehicle)
      - Lingering without visible purpose, attempting to stay out of view

      ### Critical Threat (Level 2)
      Forced entry in progress, break-in tools (crowbar, bolt cutters), weapons, violence,
      active property damage or theft.

      ### Output rules (in addition to the schema)
      - Phone notifications often show only `title` plus the first ~75 characters of
        `shortSummary`. Keep `shortSummary` at 50-75 characters (shorter than the schema
        minimum is fine), key facts first: who/what, what happened. Do not repeat the camera or
        location from `title` and do not write the threat level.
      - Start `scene` with the same kind of standalone summary sentence, then the chronological
        narrative.
      - Vehicles: give type and colour, and name any readable lettering, company name or
        carrier logo - it tells deliveries and services apart from unknown visitors.
      - `other_concerns`: list the predefined concern only when it actually applies, word for
        word. Never put your own notes there; otherwise return an empty list.
```

## Notifications

The [SgtBatten Frigate Notifications blueprint](https://github.com/SgtBatten/HA_blueprints)
(Beta, after 2026-11-13) has a `genai_notification_title` input. It sends `title` and
`shortSummary` as the GenAI update to the first alert. Example title with a threat-level prefix:

```
{{ '🚨 Security concern: ' if genai_threat_level == 2 else '⚠️ Needs review: ' if genai_threat_level == 1 else '' }}{{ genai_title }}
```
