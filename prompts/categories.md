# Email Categories

This file is the classifier's source of truth. Edit it freely — the text under
each heading is sent to the model verbatim as that category's definition, so
wording changes here directly change classification behavior.

Format:
- `## Name`   = a category. The heading text becomes the Gmail label.
- `### Name`  = a subcategory of the category above it. Label: `Category/Name`.
- `archive: yes|no` on the line under a category heading controls whether
  matching mail is moved out of the inbox (yes) or labeled in place (no).
- Everything else under a heading is that category's description (the prompt).

Anything that doesn't clearly match a category below stays in the inbox
unlabeled.

## Jobs
archive: yes

Broad category containing all emails pertaining to jobs and job applications:
recruiting, applications, interviews, offers, rejections, and related
correspondence.

### Interviews
Emails explicitly asking to schedule, confirm, or prepare for an interview.
These typically come from a person (a recruiter or coordinator) rather than an
automated system. Any kind of interview counts: in person, virtual, phone, or
online assessments (OA, HackerRank, CodeSignal, and similar).

### Rejections
Emails explicitly rejecting an application — "we've decided to move forward
with other candidates", "we will not be proceeding with your application",
and similar.

### Confirmations
Automated confirmations that an application was received or submitted —
"Your application has been received", "Thanks for applying", and similar.

### Unsure
Job-related emails that don't clearly fit interviews, rejections, or
confirmations — or where the right subcategory is genuinely uncertain.
Prefer this over guessing a more specific subcategory.

## Security
archive: yes

Emails pertaining to account security: 2FA/OTP one-time codes, "click here to
verify" messages, password resets, and new sign-in or security alerts.

## Purchases
archive: yes

Emails containing information about purchases and orders.

### Receipts
Order confirmations, receipts, and payment confirmations for purchases.

### Shipping
Shipping and delivery information: shipment notifications, tracking updates,
out-for-delivery and delivered notices.

## Finances
archive: yes

Emails pertaining to personal finances: banks, credit cards, transactions,
statements, balance alerts, and payment reminders or due-date notices.
