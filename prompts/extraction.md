You are extracting job application details from an application-confirmation
email ("your application has been received" and similar).

Extract only information explicitly present in the email. Use null for any
field that is absent or unclear — never guess or infer.

Fields:
- company: the employing company's name.
- role_title: the job/role title as written in the email.
- req_number: the requisition ID, job ID, or reference number, if present
  (e.g. "R-12345", "Job ID 98765").
- role_type: "internship", "co_op", "full_time", or "part_time" — only if the
  email states it; otherwise null.
- season: the work term if stated, e.g. "Summer 2026" or "Fall 2026".
