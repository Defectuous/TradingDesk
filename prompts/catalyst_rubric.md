<!-- The A-F catalyst rubric Newsie grades with. Edit freely; Newsie reads this file at startup.
     Code also enforces two rules after the model answers, so they hold even if the model slips:
       - a grade driven by news older than the current session drops one letter
       - an offering filed today (EDGAR S-1/S-3/F-1/F-3/424B*, 8-K item 3.02) caps the grade at D -->

=== CATALYST GRADE (A-F) ===

Grade the strongest catalyst for THIS ticker's move today:

A - major, fresh, company-changing: FDA approval / Fast Track / Breakthrough designation, large contract
    with a named partner, buyout or merger offer, earnings that blow past expectations with raised guidance.
    Published today, credible source.
B - solid but smaller: partnership, patent, positive trial data, earnings beat, notable analyst upgrade.
C - weak or vague: "exploring strategic alternatives", conference appearance, minor product update, vague
    letter to shareholders.
D - recycled or promotional: old news reposted, paid-promotion articles, press releases with no substance.
F - none or negative: no news found for the move, or the news is bearish (offering, reverse split,
    delisting notice, failed trial).

Freshness counts: news older than the current session drops at least one grade.
An offering filed the same day caps the grade at D no matter what else is out.

Negative news takes priority: if any article or filing reports an offering of any kind (public, registered
direct, private placement, PIPE, ATM, financing paid in shares or convertibles), warrant exercise or
repricing, reverse split, exchange deficiency or delisting notice, going-concern doubt, failed trial or FDA
rejection about THIS company, the grade is D or F even if there is also positive news.

=== CATALYST TYPE LABEL ===

Use exactly one of: FDA, Contract, Buyout, Earnings, Partnership, Trial data, Upgrade, Product, Patent,
Offering, Reverse split, Delisting, Other, None.
Use "None" only when there is no company-specific news at all.
