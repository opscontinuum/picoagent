# Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's private vulnerability reporting:
**Security tab → Report a vulnerability** on this repository. If that path is unavailable to
you, open an issue that says only "security report, requesting a private channel" with no
details, and the maintainer will arrange one.

What happens to a report, and the record each step leaves:

1. **Tracked** - every report gets a private advisory (or an issue, once it is safe to be
   public), which is the tracking record.
2. **Confirmed** - the maintainer reproduces it. The repository's standard for security
   claims applies to reports too: confirmation means a demonstration, ideally a failing
   test, not a plausibility argument.
3. **Remediated** - the fix lands as a pull request whose tests are proved against the
   defect (plant it, watch red; fix it, watch green), and the finding joins the threat
   model or the assessment record if it changes either.
4. **Notified** - the fix is announced through the GitHub release notes and, where GitHub
   supports it for the report, a published security advisory.

Please do not open public issues with exploit details before a fix exists.

The security documentation worth reading before reporting: `docs/security/threat-model.md`
(what is already known, ranked, including what is deliberately open) and
`docs/security/trust-boundaries.md` (where the lines are). A report that something
documented as a limit is a limit is appreciated but will be answered with the document.
