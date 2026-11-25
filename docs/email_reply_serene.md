Subject: Re: Copilot guardrail policy — confirmed and implemented

Hi Serene,

Thank you for the clear answers. We've taken your ten points and encoded them
directly into the copilot's guardrails — so they're enforced in code, not just
in the prompt. Here's what's now locked in, exactly as you specified:

1. Who can use it — outlet managers, the ops team, you, and the boss.
   Counter staff are excluded.
2. Names are fine on work messages (e.g. who reported the broken machine);
   phone numbers are never shown.
3. We show the relevant part of a message, not the whole chat.
4. Never shown at the individual level: phone numbers, personal emails,
   home addresses, NRIC numbers, and any one person's pay rate.
   Pay totals by outlet are fine.
5. Supplier names, business addresses, and GST numbers — fine to show.
6. One access level for everyone; point 4 covers pay.
7. Bulk exports are refused.
8. We keep the question/answer traces for six months, redacted under
   the same rules as point 4.
9. We refuse anything about one person's pay, performance, or discipline,
   and any legal or tax advice — and route those to you. On prices we
   show the numbers, but leave the decision to the boss.
10. Confirmed read-only: the copilot only reads. No changes, no orders,
    no outbound messages.

We've verified all ten rules with a test suite (28 cases covering each
rule plus the PII redaction and read-only guarantees), and all are passing.
The demo is on track for 27 Nov at NTU SR902.

If you'd like any wording tightened or a rule adjusted before then, let me
know and I'll fold it in.

Best regards,
Mike Yap
Kaya Kopi Copilot team
