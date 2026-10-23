# Deterministic computed-value availability rule

Scan only existing classify task-success answers for N04/R06, using the actual gold claim IDs, both arms and all five repetitions. Exclude warmups. Inspect every original statement independently. Its citations, answer text and judge grades are never changed.

A candidate requires a listed field alias directly joined to an availability predicate as subject, predicate object or adjective. A coordinated list may name several fields. Each matched field is checked against that same field in row.actual_tool_result; null never flags and zero is non-null. Gold expected results/status and owner corrections are not value authorities or eligibility overrides.

The machine rule below enumerates every alias, relation and guard. Specific latest-date aliases precede contained shorter aliases. Whole-sentence punctuation bounds the local guard: recognised conditional, negated/reporting and non-value complement forms are excluded. No field is inferred from another field, a generic unknown word, a pronoun, a date/amount or a different statement. Offsets are original Unicode code points with exclusive ends. Lexical matches are candidates for the owner, not semantic verdicts.

The finite aliases cannot exhaust English. Ambiguous bare deadline/amount/earliest increase date and unlisted paraphrases are not matched. Unusual quotations/negation can still produce false positives or misses. Zero hits would not establish semantic correctness. The owner reads every flagged full answer before any audited count.

Serving source and all input hashes remain distinct from the scan implementation file hashes in scan.json. No calibration, serving configuration or original evidence is rebound to this audit-code commit. Output contains only flagged answer material, coverage and binding metadata, never raw provider messages or owner-record copies.

~~~json
{
  "authority": "row.actual_tool_result; non-null test, including zero",
  "cases": [
    "N04",
    "R06"
  ],
  "clause_boundaries": "[.!?;\\n]",
  "conditional_guard": "\\b(?:if|unless|suppos(?:e|ing)|hypothetically|would|could|might)\\b",
  "eligibility": "existing classify task-success; actual gold claim IDs; all arms/repeats",
  "field_aliases": {
    "cap_cents_exact": [
      "cap_cents_exact",
      "(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)",
      "(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?"
    ],
    "deemed_served_on": [
      "deemed_served_on",
      "deemed service date",
      "deemed served date",
      "date of deemed service",
      "date (?:the )?notice (?:is|was) deemed served"
    ],
    "earliest_notice_on": [
      "earliest_notice_on",
      "earliest (?:permitted |possible |allowable )?(?:effective|notice) date",
      "earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule"
    ],
    "earliest_spacing_on": [
      "earliest_spacing_on",
      "(?:earliest )?spacing date",
      "earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule"
    ],
    "guideline_percent": [
      "guideline_percent",
      "guideline (?:percentage|percent|rate)"
    ],
    "latest_deemed_service_on": [
      "latest_deemed_service_on",
      "latest (?:deemed )?service date",
      "latest date (?:for|of) (?:deemed )?service"
    ],
    "latest_dispatch_on": [
      "latest_dispatch_on",
      "latest (?:dispatch|mailing|sending) date",
      "(?:dispatch|mailing|sending) deadline",
      "latest date to (?:dispatch|mail|send)"
    ],
    "notice_days": [
      "notice_days",
      "(?:actual )?notice (?:days|interval|length|period)",
      "(?:number of |length of )?(?:calendar )?days of notice"
    ]
  },
  "flags": "IGNORECASE; original Unicode code-point offsets, end exclusive",
  "limits": "Finite English aliases and direct predicates only; no pronoun/coreference, implicit denial, generic deadline/amount, ambiguous earliest-increase date, or semantic inference. Coordinated subjects must each name a known field. Conditional clauses and recognised negated/reporting frames are excluded. Other quotations, unusual negation and paraphrases can be missed or overflagged. A candidate is not an error or an audited correction.",
  "negated_frame_guard": "\\b(?:(?:does|do|did) not (?:mean|imply|say|claim)|not true that|(?:wrong|incorrect|false) to (?:say|claim)|never (?:said|claimed))\\b",
  "negated_prefix_guard": "\\b(?:not|never)\\s+(?:(?:really|actually|necessarily)\\s+)*$",
  "nonvalue_field_suffix_guard": "(?:['\\u2019]s)?\\s+(?:validity|applicability|compliance|requirements?|eligibility|status)\\b",
  "nonvalue_suffix_guard": "\\s+(?:to be|whether|if)\\b",
  "relations": {
    "adjective": "\\b(?P<predicate>(?:unknown|undetermined|unavailable))\\s+(?:the\\s+)?(?P<fields>(?:[*_]{1,2}|`)?(?<!\\w)(?:latest_deemed_service_on|latest (?:deemed )?service date|latest date (?:for|of) (?:deemed )?service|latest_dispatch_on|latest (?:dispatch|mailing|sending) date|(?:dispatch|mailing|sending) deadline|latest date to (?:dispatch|mail|send)|earliest_spacing_on|(?:earliest )?spacing date|earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule|earliest_notice_on|earliest (?:permitted |possible |allowable )?(?:effective|notice) date|earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule|deemed_served_on|deemed service date|deemed served date|date of deemed service|date (?:the )?notice (?:is|was) deemed served|notice_days|(?:actual )?notice (?:days|interval|length|period)|(?:number of |length of )?(?:calendar )?days of notice|guideline_percent|guideline (?:percentage|percent|rate)|cap_cents_exact|(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)|(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?)(?!\\w)(?:[*_]{1,2}|`)?(?:\\s*(?:,\\s*(?:(?:and|or)\\s+)?|\\s+(?:and|or)\\s+)(?:[*_]{1,2}|`)?(?<!\\w)(?:latest_deemed_service_on|latest (?:deemed )?service date|latest date (?:for|of) (?:deemed )?service|latest_dispatch_on|latest (?:dispatch|mailing|sending) date|(?:dispatch|mailing|sending) deadline|latest date to (?:dispatch|mail|send)|earliest_spacing_on|(?:earliest )?spacing date|earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule|earliest_notice_on|earliest (?:permitted |possible |allowable )?(?:effective|notice) date|earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule|deemed_served_on|deemed service date|deemed served date|date of deemed service|date (?:the )?notice (?:is|was) deemed served|notice_days|(?:actual )?notice (?:days|interval|length|period)|(?:number of |length of )?(?:calendar )?days of notice|guideline_percent|guideline (?:percentage|percent|rate)|cap_cents_exact|(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)|(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?)(?!\\w)(?:[*_]{1,2}|`)?)*)",
    "field_subject": "(?P<fields>(?:[*_]{1,2}|`)?(?<!\\w)(?:latest_deemed_service_on|latest (?:deemed )?service date|latest date (?:for|of) (?:deemed )?service|latest_dispatch_on|latest (?:dispatch|mailing|sending) date|(?:dispatch|mailing|sending) deadline|latest date to (?:dispatch|mail|send)|earliest_spacing_on|(?:earliest )?spacing date|earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule|earliest_notice_on|earliest (?:permitted |possible |allowable )?(?:effective|notice) date|earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule|deemed_served_on|deemed service date|deemed served date|date of deemed service|date (?:the )?notice (?:is|was) deemed served|notice_days|(?:actual )?notice (?:days|interval|length|period)|(?:number of |length of )?(?:calendar )?days of notice|guideline_percent|guideline (?:percentage|percent|rate)|cap_cents_exact|(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)|(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?)(?!\\w)(?:[*_]{1,2}|`)?(?:\\s*(?:,\\s*(?:(?:and|or)\\s+)?|\\s+(?:and|or)\\s+)(?:[*_]{1,2}|`)?(?<!\\w)(?:latest_deemed_service_on|latest (?:deemed )?service date|latest date (?:for|of) (?:deemed )?service|latest_dispatch_on|latest (?:dispatch|mailing|sending) date|(?:dispatch|mailing|sending) deadline|latest date to (?:dispatch|mail|send)|earliest_spacing_on|(?:earliest )?spacing date|earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule|earliest_notice_on|earliest (?:permitted |possible |allowable )?(?:effective|notice) date|earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule|deemed_served_on|deemed service date|deemed served date|date of deemed service|date (?:the )?notice (?:is|was) deemed served|notice_days|(?:actual )?notice (?:days|interval|length|period)|(?:number of |length of )?(?:calendar )?days of notice|guideline_percent|guideline (?:percentage|percent|rate)|cap_cents_exact|(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)|(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?)(?!\\w)(?:[*_]{1,2}|`)?)*)\\s*(?::|=)?\\s*(?:(?:is|are|was|were|remains?|stays?)\\s+)?(?:(?:currently|presently|still|yet|now)\\s+)*(?P<predicate>(?:[*_]{1,2}|`)?(?:(?:unknown|undetermined|unavailable)|not\\s+(?:(?:currently|presently|still|yet|now)\\s+)*(?:known|available|(?:determined|calculated|computed|established|identified|ascertained|confirmed))|(?:cannot|can['\\u2019]t|can not)\\s+(?:(?:currently|presently|still|yet|now)\\s+)*be\\s+(?:determined|calculated|computed|established|identified|ascertained|confirmed))\\b(?:[*_]{1,2}|`)?)",
    "predicate_object": "\\b(?P<predicate>(?:(?:cannot|can['\\u2019]t|can not)\\s+(?:(?:currently|presently|still|yet|now)\\s+)*(?:determine|calculate|compute|establish|identify|ascertain|confirm)|unable to (?:determine|calculate|compute|establish|identify|ascertain|confirm)|(?:not enough|insufficient) (?:information|facts|data) to (?:determine|calculate|compute|establish|identify|ascertain|confirm)))\\s+(?:the\\s+)?(?P<fields>(?:[*_]{1,2}|`)?(?<!\\w)(?:latest_deemed_service_on|latest (?:deemed )?service date|latest date (?:for|of) (?:deemed )?service|latest_dispatch_on|latest (?:dispatch|mailing|sending) date|(?:dispatch|mailing|sending) deadline|latest date to (?:dispatch|mail|send)|earliest_spacing_on|(?:earliest )?spacing date|earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule|earliest_notice_on|earliest (?:permitted |possible |allowable )?(?:effective|notice) date|earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule|deemed_served_on|deemed service date|deemed served date|date of deemed service|date (?:the )?notice (?:is|was) deemed served|notice_days|(?:actual )?notice (?:days|interval|length|period)|(?:number of |length of )?(?:calendar )?days of notice|guideline_percent|guideline (?:percentage|percent|rate)|cap_cents_exact|(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)|(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?)(?!\\w)(?:[*_]{1,2}|`)?(?:\\s*(?:,\\s*(?:(?:and|or)\\s+)?|\\s+(?:and|or)\\s+)(?:[*_]{1,2}|`)?(?<!\\w)(?:latest_deemed_service_on|latest (?:deemed )?service date|latest date (?:for|of) (?:deemed )?service|latest_dispatch_on|latest (?:dispatch|mailing|sending) date|(?:dispatch|mailing|sending) deadline|latest date to (?:dispatch|mail|send)|earliest_spacing_on|(?:earliest )?spacing date|earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule|earliest_notice_on|earliest (?:permitted |possible |allowable )?(?:effective|notice) date|earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule|deemed_served_on|deemed service date|deemed served date|date of deemed service|date (?:the )?notice (?:is|was) deemed served|notice_days|(?:actual )?notice (?:days|interval|length|period)|(?:number of |length of )?(?:calendar )?days of notice|guideline_percent|guideline (?:percentage|percent|rate)|cap_cents_exact|(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)|(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?)(?!\\w)(?:[*_]{1,2}|`)?)*)"
  },
  "version": "computed-value-availability-v1"
}
~~~
