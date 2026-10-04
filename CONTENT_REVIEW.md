# Phase 1 content review

All content is in `web/src/content/`. The assistant will answer ONLY from these files, so errors here become
errors in its answers. Skim everything, and check the items marked **CHECK**.

## What was written (23 files, about 6,000 words)

| Folder | Files |
|---|---|
| `pages/` | about, contact, faqs, privacy |
| `practice/` | corporate-commercial, real-estate-tenancy, tax, ip-data-protection, dispute-resolution |
| `people/` | Tomi Kendot (MP), Ifeanyi Obi, Halima Sani, Chidera Eze, Bayo Adewale |
| `insights/` | 9 articles (listed below) |

## Fact-check status of the articles

**Checked word for word against the statute text we hold** (naija-law-rag corpus):

| Article | Source |
|---|---|
| Notice to quit in Lagos | Lagos Tenancy Law 2011, s.1(3), s.13(1)-(6), s.16 |
| Advance rent in Lagos | Lagos Tenancy Law 2011, s.4(1)-(5) |
| Rent receipts, deposits, unlawful eviction | Lagos Tenancy Law 2011, s.5, s.10, s.44 |
| Company tax filing deadlines | Nigeria Tax Administration Act 2025 (return deadlines, 31 Jan employer return, record keeping) |
| Your rights if arrested | Constitution 1999, s.35(2)-(5), s.36 |
| Nigeria Tax Act 2025 for small businesses | Your `nigerian_tax.txt` summary (bands, 4% levy, 7.5% VAT, rent relief) |

**CHECK: written from general knowledge, not from a statute file we hold.** These are standard, widely published
points, but verify them (or have a lawyer friend skim them) before a real prospect sees them:

- `ndpa-2023-basics.md`: 6 lawful bases, 72-hour breach notice to the NDPC, registration and a DPO for businesses "of major
  importance", penalty caps (the higher of ₦10m or 2%, or the higher of ₦2m or 2%).
- `registering-a-company-in-nigeria.md`: single-member private companies, ₦100,000 minimum issued share capital,
  one director for small companies (CAMA 2020).
- `registering-a-trademark-in-nigeria.md`: 7-year initial term and 14-year renewals, one class per application.
  (I deliberately did not state the length of the opposition period, because I'm not certain of it.)

**One deliberate omission:** the small-company threshold under the Nigeria Tax Act. Your `nigerian_tax.txt` says the turnover limit is ₦50m, but other
published summaries give a different figure, so the article says "below the thresholds set in the Act" rather than
state a number that might be wrong. (Worth checking which figure is right, and fixing `naija-law-rag` too if needed.)

## Built-in test material for Phase 5 (evaluation)

The content was written so the evaluation has something real to check against:
- **What the firm does not do:** no criminal defence, family law or immigration work. The assistant must say so, not attempt an answer.
- **Firm facts:** consultation fee (₦50,000, 45 minutes, deducted from the first invoice), office hours, reply within one working day, languages.
- **Paraphrase targets:** e.g. "can my landlord collect 2 years rent?" must find the advance-rent article, which never uses those words.
- **Nearby questions it can't answer:** e.g. Abuja tenancy rules, the trademark opposition period, and the small-company turnover figure
  are deliberately NOT in the content. The assistant must decline these.

## Fictional-firm safeguards

- Every page about the firm or its people says it is fictional.
- Email addresses use the reserved `.example` domain. Phone numbers are marked as demo numbers.
- Addresses are invented ("12 Kendot Close").
