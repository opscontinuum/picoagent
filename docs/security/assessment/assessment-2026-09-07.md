# picoagent — Application Security and Development STIG Assessment

**Benchmark:** DISA Application Security and Development STIG, V6R4 (Release: 4, Benchmark Date: 01 Oct 2025), 286 rules, assessed from `U_ASD_STIG_V6R4_Manual-xccdf.xml`.
**Artifact:** picoagent working tree at head commit `3645b9f`, a standard-library-only Python coding-agent harness — a locally-run single-user developer CLI, not a hosted application.
**Assessed:** 2026-09-07, by automated code review, execution of the repository's own stig-runner probes, targeted verification scripts, and a full run of the 972-test suite (`OK, 5 skipped` — reproduced during this assessment).
**Assessor:** automated; see "What this assessment is not" before treating any determination as signable.

## 1. Executive summary

| Status | CAT I (high) | CAT II (medium) | CAT III (low) | Total |
|---|---|---|---|---|
| **Open** | 1 | 6 | 1 | 8 |
| **NotAFinding** | 8 | 44 | 5 | 57 |
| **Not_Applicable** | 22 | 155 | 14 | 191 |
| **Not_Reviewed** | 3 | 25 | 2 | 30 |
| **Total** | 34 | 230 | 22 | 286 |

### The findings that actually matter

**One CAT I Open — V-222604 (APSC-DV-002510, command injection).** The product's core
function is executing shell commands composed by the model, and model output is untrusted
input by the project's own trust model. In the shipped default there is no gate of any kind
between that untrusted command string and `/bin/sh` — no confirmation, no allowlist, no
policy. Every mitigation (permission-gate, `confine_to_project`, `api.set_active_tools`,
credential-guard) is opt-in. A prompt-injected document in any repository the agent reads can
drive arbitrary command execution as the user, out of the box. The fix is not exotic: ship an
execution-authorization gate as the default and make running open a documented deviation.

**One defect, three CAT II Opens — the session log.** The session log is simultaneously the
application's stored data and its only audit trail, and it is created world-readable (0644,
directory 0755 — verified by execution) while the codebase carefully restricts the
credentials file, the trust store and even temp-file spills to 0600. Compounding it, the
built-in shell hands the **entire process environment** to every command the model runs
(also verified by execution: a planted key was echoed straight into a tool result, which is
persisted and replayed to the model). That is V-222500 (audit info read access), V-222587
(stored info confidentiality) and V-222444 (sensitive data in logs). The credential-guard
plugin's own docstring names this env-to-log path as "the actual leak path this plugin
closes" — but it is an example plugin, not a default. Fix: owner-only session files/dirs,
and make the environment allowlist the built-in shell's default.

**Supply chain — V-222513 (APSC-DV-001430).** The plugin trust store (SHA-256 over every
file, per-file diffs, commit provenance, fail-closed on damage) is genuinely good change
*detection*, but nothing anywhere verifies a **digital signature** from a recognized
authority: plugins arrive by `git clone` from whatever host the spec names, git tag/commit
signatures are never checked, and plugin `python_deps` are pip-installed without hash pinning.

**Documentation — V-222655 (threat model)** is Open on the repository's own admission
("Not yet written. Listed so the gaps are visible rather than implied"). V-222469 (shutdown
not logged) and V-222649 (no coverage statistics) round out the Opens; both are small.

### Where the codebase is genuinely strong

The clean determinations are not charity; they cite lines. Fail-closed state handling (a
torn trust store means *nothing* is trusted, not everything), atomic writes, same-origin
redirect refusal that protects both the Authorization header and the request body, a
single path-resolution seam shared by tools and guards precisely because separate
resolution was a bypass twice, hostile-input hardening down to metaclass `__name__` traps
and lone surrogates, per-push Bandit/Semgrep/Trivy in CI, and 191 rules Not Applicable for
reasons the rules' own check text supports — not because the tool is small.

---

## 2. CAT I — all 34 rules, individually

### V-222399 / APSC-DV-000190 (CAT I) — **Not_Applicable**

*Messages protected with WS_Security must use time stamps with creation and expiration times.*

The rule's own check content makes it NA when the application does not use SOAP, WS-Security or SAML. picoagent speaks only OpenAI-dialect JSON over HTTP(S) to a user-configured model endpoint; there is no SOAP, WS-Security, or SAML anywhere in the codebase.

**Evidence:** picoagent/core/provider.py:259-293 (JSON POST to {base_url}/chat/completions is the only protocol); grep for SOAP/SAML/WSS across the tree returns nothing outside the STIG rule text fixtures

### V-222400 / APSC-DV-000200 (CAT I) — **Not_Applicable**

*Validity periods must be verified on all application messages using WS-Security or SAML assertions.*

The rule's own check content makes it NA when the application does not use SOAP, WS-Security or SAML. picoagent speaks only OpenAI-dialect JSON over HTTP(S) to a user-configured model endpoint; there is no SOAP, WS-Security, or SAML anywhere in the codebase.

**Evidence:** picoagent/core/provider.py:259-293 (JSON POST to {base_url}/chat/completions is the only protocol); grep for SOAP/SAML/WSS across the tree returns nothing outside the STIG rule text fixtures

### V-222403 / APSC-DV-000230 (CAT I) — **Not_Applicable**

*The application must use the NotOnOrAfter condition when using the SubjectConfirmation element in a SAML assertion.*

The rule's own check content makes it NA when the application does not use SOAP, WS-Security or SAML. picoagent speaks only OpenAI-dialect JSON over HTTP(S) to a user-configured model endpoint; there is no SOAP, WS-Security, or SAML anywhere in the codebase.

**Evidence:** picoagent/core/provider.py:259-293 (JSON POST to {base_url}/chat/completions is the only protocol); grep for SOAP/SAML/WSS across the tree returns nothing outside the STIG rule text fixtures

### V-222404 / APSC-DV-000240 (CAT I) — **Not_Applicable**

*The application must use both the NotBefore and NotOnOrAfter elements or OneTimeUse element when using the Conditions element in a SAML assertion.*

The rule's own check content makes it NA when the application does not use SOAP, WS-Security or SAML. picoagent speaks only OpenAI-dialect JSON over HTTP(S) to a user-configured model endpoint; there is no SOAP, WS-Security, or SAML anywhere in the codebase.

**Evidence:** picoagent/core/provider.py:259-293 (JSON POST to {base_url}/chat/completions is the only protocol); grep for SOAP/SAML/WSS across the tree returns nothing outside the STIG rule text fixtures

### V-222425 / APSC-DV-000460 (CAT I) — **Not_Applicable**

*The application must enforce approved authorizations for logical access to information and system resources in accordance with applicable access control policies.*

Access-control and privileged-function rules presuppose application-managed subjects, objects, roles, or privilege tiers. picoagent manages none: every action it takes runs strictly as the one invoking OS user, and the operating system enforces authorization on each file and process access. There is no non-privileged/privileged distinction inside the application to enforce or audit. The one genuinely novel principal - the model - is not a user; the controls the design offers over it (tool_call guard events, confine_to_project, api.set_active_tools, permission-gate) are assessed under V-222604, where the default-configuration gap is recorded as Open.

**Evidence:** docs/security/trust-boundaries.md:387-403 (confinement model); picoagent/core/tools.py:153-176 (resolve_path contract)

### V-222430 / APSC-DV-000510 (CAT I) — **NotAFinding**

*The application must execute without excessive account permissions.*

CAT I. The application runs without excessive permissions: it is installed and run entirely as an unprivileged user (venv pip install), demands no elevation, contains no setuid/sudo/privilege-escalation code, uses no service accounts and connects to no database. Children it spawns are placed in their own process groups and killed as trees rather than being left running.

**Evidence:** README.md:9-13 (user-level venv install); pyproject.toml; picoagent/core/tools.py:280-312 (own_process_group/spawn_shell), 342-376 (kill_process_tree); grep: no setuid/sudo calls in the codebase (the only "sudo" match is permission-gate's dangerous-command pattern)

### V-222432 / APSC-DV-000530 (CAT I) — **Not_Applicable**

*The application must enforce the limit of three consecutive invalid logon attempts by a user during a 15 minute time period.*

picoagent has no user accounts of any kind: no account store, no creation/disable/removal functions, no default or built-in accounts, no passwords to default. It is a process the already-OS-authenticated user starts; identity and account management are wholly the operating system's. The account-lifecycle events this rule audits or constrains cannot occur in the application. (For V-222662: the probe for APSC-DV-003280 returned zero hits; no credentials of any kind ship with the product.)

**Evidence:** No account-management code exists anywhere under picoagent/ or examples/plugins/; probe APSC-DV-003280: 0 hits

### V-222522 / APSC-DV-001540 (CAT I) — **Not_Applicable**

*The application must uniquely identify and authenticate organizational users (or processes acting on behalf of organizational users).*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222536 / APSC-DV-001680 (CAT I) — **Not_Applicable**

*The application must enforce a minimum 15-character password length.*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222542 / APSC-DV-001740 (CAT I) — **Not_Applicable**

*The application must only store cryptographic representations of passwords.*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222543 / APSC-DV-001750 (CAT I) — **Not_Applicable**

*The application must transmit only cryptographically-protected passwords.*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222550 / APSC-DV-001810 (CAT I) — **Not_Applicable**

*The application, when utilizing PKI-based authentication, must validate certificates by constructing a certification path (which includes status information) to an accepted trust anchor.*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222551 / APSC-DV-001820 (CAT I) — **Not_Applicable**

*The application, when using PKI-based authentication, must enforce authorized access to the corresponding private key.*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222554 / APSC-DV-001850 (CAT I) — **NotAFinding**

*The application must not display passwords/PINs as clear text.*

CAT I. The one place the application accepts a secret interactively is /secrets set, which reads via getpass (never echoed to the terminal), refuses to prompt without a real TTY, and thereafter displays only a last-4-characters mask. Slash commands short-circuit before session logging, so the typed key is never recorded; provider error text is scrubbed of the key before display.

**Evidence:** examples/plugins/credential-guard/credential_guard.py:365-369 (_prompt_secret/getpass), 166-168 (mask), 391-404; picoagent/core/loop.py:101-104 (commands bypass append_message); picoagent/core/provider.py:357-367 (_scrub)

### V-222555 / APSC-DV-001860 (CAT I) — **Not_Applicable**

*The application must use mechanisms meeting the requirements of applicable federal laws, Executive Orders, directives, policies, regulations, standards, and guidance for authentication to a cryptographic module.*

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

### V-222577 / APSC-DV-002230 (CAT I) — **Not_Applicable**

*The application must not expose session IDs.*

picoagent is a locally-run single-user CLI process. It performs no user logon, issues no session identifiers or cookies, and maintains no authenticated communication sessions; the "session" in picoagent is a local JSONL conversation log, not an authentication session. The events and artifacts this rule regulates do not exist in the application.

**Evidence:** picoagent/cli.py:721-730 (main: no auth path); picoagent/core/session.py:1-15 (session = append-only JSONL conversation log); no cookie/session-token code exists in the codebase (probe APSC-DV-000010/000060 hits are all docstrings/tests)

### V-222578 / APSC-DV-002240 (CAT I) — **Not_Applicable**

*The application must destroy the session ID value and/or cookie on logoff or browser close.*

picoagent is a locally-run single-user CLI process. It performs no user logon, issues no session identifiers or cookies, and maintains no authenticated communication sessions; the "session" in picoagent is a local JSONL conversation log, not an authentication session. The events and artifacts this rule regulates do not exist in the application.

**Evidence:** picoagent/cli.py:721-730 (main: no auth path); picoagent/core/session.py:1-15 (session = append-only JSONL conversation log); no cookie/session-token code exists in the codebase (probe APSC-DV-000010/000060 hits are all docstrings/tests)

### V-222585 / APSC-DV-002310 (CAT I) — **NotAFinding**

*The application must fail to a secure state if system initialization fails, shutdown fails, or aborts fail.*

CAT I, assessed against intent with code-and-test evidence rather than live fault injection. Failure states land closed, not open: a trust store that cannot be read is treated as EMPTY (nothing loads until re-approved) rather than as "everything is still approved"; store writes are atomic (temp+fsync+rename) so no reader sees a torn write; a session log with a hole mid-file refuses to resume rather than silently sending a rewritten conversation to the model; a required security plugin that fails to load stops the session with a distinct exit code; the user's own unreadable config stops startup rather than running under settings they did not choose.

**Evidence:** picoagent/plugins/loader.py:465-509 (_read fail-closed, documented), 663-694 (atomic _save); picoagent/core/session.py:50-96 (torn-line vs mid-file damage); picoagent/cli.py:47-56 (exit codes 3/4/5); picoagent/core/config.py:456-461; tests/test_torn_state_files.py, tests/test_config_refusals.py (972-test suite passes: verified this assessment, "Ran 972 tests ... OK (skipped=5)")

### V-222588 / APSC-DV-002340 (CAT I) — **Not_Reviewed**

*The application must implement approved cryptographic mechanisms to prevent unauthorized modification of organization-defined information at rest on organization-defined information system components.*

These rules turn on data-protection requirements the information owner defines, and no such definition exists for this artifact (the security README lists the threat model as unwritten; no data-categorization statement exists). The facts available: picoagent stores conversation history, tool output, and configuration in plaintext files; the only cryptographic at-rest protection in the tree is OS permission bits (0600 on the credentials store). If a deployment processes CUI, at-rest cryptography must come from the host (FDE/encrypted volumes) and be documented; the tool itself provides none. To close: obtain the data owner's protection requirements and the hosting environment's at-rest encryption evidence.

**Evidence:** picoagent/core/session.py (plaintext JSONL); examples/plugins/credential-guard/credential_guard.py:125-163

### V-222589 / APSC-DV-002350 (CAT I) — **Not_Reviewed**

*The application must use appropriate cryptography in order to protect stored DOD information when required by the information owner or DOD policy.*

These rules turn on data-protection requirements the information owner defines, and no such definition exists for this artifact (the security README lists the threat model as unwritten; no data-categorization statement exists). The facts available: picoagent stores conversation history, tool output, and configuration in plaintext files; the only cryptographic at-rest protection in the tree is OS permission bits (0600 on the credentials store). If a deployment processes CUI, at-rest cryptography must come from the host (FDE/encrypted volumes) and be documented; the tool itself provides none. To close: obtain the data owner's protection requirements and the hosting environment's at-rest encryption evidence.

**Evidence:** picoagent/core/session.py (plaintext JSONL); examples/plugins/credential-guard/credential_guard.py:125-163

### V-222596 / APSC-DV-002440 (CAT I) — **NotAFinding**

*The application must protect the confidentiality and integrity of transmitted information.*

Transmitted information (the conversation, tool results, and the API key) is protected in the shipped default configuration: the default endpoint is https://api.openai.com/v1 with certificate validation on (platform ssl default), non-HTTP(S) schemes are refused so file:/ftp:/data: can never be an endpoint, and redirects are followed only within the exact origin (scheme+host+port) - a cross-origin or https->http redirect is refused outright rather than followed with headers, a stronger stance than requests/curl, demonstrated against a live second server per the docs. Preparation and reception happen in process memory feeding directly into that channel. CAVEAT for the deployer, stated because the check is configuration-dependent: http:// endpoints are deliberately permitted (local Ollama/vLLM is the headline use), and nothing warns when an http URL points at a NON-loopback host - a deployment must configure https for any remote endpoint, and a warning for remote-http would be a worthwhile hardening.

**Evidence:** picoagent/core/provider.py:128-156 (HTTP_SCHEMES/check_base_url), 159-239 (_SameOriginRedirects, _OPENER), 270-271 (https default); docs/security/trust-boundaries.md:365-381; tests/test_plugin_redirects.py

### V-222601 / APSC-DV-002485 (CAT I) — **Not_Applicable**

*The application must not store sensitive information in hidden fields.*

Not present in this artifact, per each rule's own scoping: no org-defined security attributes/data labeling (222393-5); no remote-access sessions INTO the application (222396-7 - the outbound model call is assessed at V-222596); no data-mining surface (222424); no tiered UI/storage architecture or web/database servers (222574, 222620, 222635, 222671); no XML processing in core and no web services (222593, 222608, 222625); no HTML/web UI, so no XSS/CSRF/hidden-field surface (222601, 222602, 222603 - the terminal-output analog, ANSI-escape injection, is affirmatively defended: picoagent/core/text.py strip_terminal_controls, tested); no SQL anywhere (222607); no mobile code (222618, 222665); no classified processing or production databases (222664, 222666, 265634); no HA/web-service redundancy requirement (222595). One caveat recorded for 222608: the stig-runner example plugin parses user-named CKL XML with stdlib xml.etree, which does not resolve external entities; internal-entity blowups are bounded by the bundled Expat >= 2.4 amplification limits.

**Evidence:** examples/plugins/stig-runner/ckl.py:41 (the only XML use, an opt-in example plugin); picoagent/core/text.py:520-535

### V-222602 / APSC-DV-002490 (CAT I) — **Not_Applicable**

*The application must protect from Cross-Site Scripting (XSS) vulnerabilities.*

Not present in this artifact, per each rule's own scoping: no org-defined security attributes/data labeling (222393-5); no remote-access sessions INTO the application (222396-7 - the outbound model call is assessed at V-222596); no data-mining surface (222424); no tiered UI/storage architecture or web/database servers (222574, 222620, 222635, 222671); no XML processing in core and no web services (222593, 222608, 222625); no HTML/web UI, so no XSS/CSRF/hidden-field surface (222601, 222602, 222603 - the terminal-output analog, ANSI-escape injection, is affirmatively defended: picoagent/core/text.py strip_terminal_controls, tested); no SQL anywhere (222607); no mobile code (222618, 222665); no classified processing or production databases (222664, 222666, 265634); no HA/web-service redundancy requirement (222595). One caveat recorded for 222608: the stig-runner example plugin parses user-named CKL XML with stdlib xml.etree, which does not resolve external entities; internal-entity blowups are bounded by the bundled Expat >= 2.4 amplification limits.

**Evidence:** examples/plugins/stig-runner/ckl.py:41 (the only XML use, an opt-in example plugin); picoagent/core/text.py:520-535

### V-222604 / APSC-DV-002510 (CAT I) — **Open**

*The application must protect from command injection.*

CAT I. The application's core function is executing shell commands composed by the model, and model output is untrusted input by the project's own trust model ("Untrusted: treat as data, never as instructions ... tool calls are requests, not orders"). In the shipped default configuration nothing stands between an untrusted command string and /bin/sh: the built-in shell tool executes whatever arrives, with no confirmation, no allowlist, and no policy gate. The mitigations exist but are all opt-in: permission-gate (an example plugin, not installed by default, and a regex denylist when it is), api.set_active_tools, confine_to_project (off by default, and explicitly not covering shell). A prompt-injected document in any repository the agent reads can therefore drive arbitrary command execution as the user - the STIG meaning of command injection - out of the box. WHAT TO CHANGE: make an execution authorization gate the default (ship permission-gate enabled and required, or build an ask-by-default confirmation into the built-in shell tool), and offer an allowlist mode for accredited deployments; document the deviation for any environment that chooses to run open.

**Evidence:** picoagent/core/tools.py:396-412 (ShellTool.execute: no gate); docs/security/trust-boundaries.md:20-27 (model output untrusted), 404-408 ("This does not confine the shell tool"), 513-515 ("Prompt injection is not solved"); examples/plugins/permission-gate/ (opt-in example, regex denylist DEFAULT_DANGEROUS covers 6 patterns)

### V-222607 / APSC-DV-002540 (CAT I) — **Not_Applicable**

*The application must not be vulnerable to SQL Injection.*

Not present in this artifact, per each rule's own scoping: no org-defined security attributes/data labeling (222393-5); no remote-access sessions INTO the application (222396-7 - the outbound model call is assessed at V-222596); no data-mining surface (222424); no tiered UI/storage architecture or web/database servers (222574, 222620, 222635, 222671); no XML processing in core and no web services (222593, 222608, 222625); no HTML/web UI, so no XSS/CSRF/hidden-field surface (222601, 222602, 222603 - the terminal-output analog, ANSI-escape injection, is affirmatively defended: picoagent/core/text.py strip_terminal_controls, tested); no SQL anywhere (222607); no mobile code (222618, 222665); no classified processing or production databases (222664, 222666, 265634); no HA/web-service redundancy requirement (222595). One caveat recorded for 222608: the stig-runner example plugin parses user-named CKL XML with stdlib xml.etree, which does not resolve external entities; internal-entity blowups are bounded by the bundled Expat >= 2.4 amplification limits.

**Evidence:** examples/plugins/stig-runner/ckl.py:41 (the only XML use, an opt-in example plugin); picoagent/core/text.py:520-535

### V-222608 / APSC-DV-002550 (CAT I) — **Not_Applicable**

*The application must not be vulnerable to XML-oriented attacks.*

Not present in this artifact, per each rule's own scoping: no org-defined security attributes/data labeling (222393-5); no remote-access sessions INTO the application (222396-7 - the outbound model call is assessed at V-222596); no data-mining surface (222424); no tiered UI/storage architecture or web/database servers (222574, 222620, 222635, 222671); no XML processing in core and no web services (222593, 222608, 222625); no HTML/web UI, so no XSS/CSRF/hidden-field surface (222601, 222602, 222603 - the terminal-output analog, ANSI-escape injection, is affirmatively defended: picoagent/core/text.py strip_terminal_controls, tested); no SQL anywhere (222607); no mobile code (222618, 222665); no classified processing or production databases (222664, 222666, 265634); no HA/web-service redundancy requirement (222595). One caveat recorded for 222608: the stig-runner example plugin parses user-named CKL XML with stdlib xml.etree, which does not resolve external entities; internal-entity blowups are bounded by the bundled Expat >= 2.4 amplification limits.

**Evidence:** examples/plugins/stig-runner/ckl.py:41 (the only XML use, an opt-in example plugin); picoagent/core/text.py:520-535

### V-222609 / APSC-DV-002560 (CAT I) — **NotAFinding**

*The application must not be subject to input handling vulnerabilities.*

CAT I. No input-handling vulnerability was found, and the codebase shows deliberate, adversarially-reviewed handling of hostile input at every boundary this assessment traced: hostile TOML (UnicodeDecodeError/RecursionError paths closed), hostile exception objects (__str__/__format__/metaclass __name__ all guarded), lone surrogates from MCP replies, ANSI/OSC terminal injection, torn state files, and path respellings. CI runs Bandit/Semgrep/Trivy per push as the scanning the check asks about. Caveat: assessed from code, tests (972 passing) and CI configuration; no independent fuzzing campaign was run for this assessment. The deliberate execution of model-supplied commands is recorded at V-222604, not here.

**Evidence:** picoagent/core/text.py:577-610 (describe_exception); picoagent/core/config.py:401-436; docs/security/trust-boundaries.md:517-596; .github/workflows/security-scan.yml; commit history: "adversarial reviews" closure commits (7095cd2, 8a5d9fe, c678664, f684b78)

### V-222612 / APSC-DV-002590 (CAT I) — **NotAFinding**

*The application must not be vulnerable to overflow attacks.*

CAT I. The artifact is pure Python on a memory-safe runtime: no C extensions, no native code, no buffer arithmetic anywhere in the tree, so classical buffer/stack/heap/format-string overflows are managed by the CPython runtime. The Python-level analogs are handled: RecursionError from attacker-nested TOML is caught deliberately (with the stack-unwound-by-then reasoning documented), and integer handling is arbitrary-precision by language definition.

**Evidence:** find: zero .c/.cpp/.rs/native files in picoagent/ or examples/; picoagent/core/config.py:414-422 (RecursionError handling, documented)

### V-222620 / APSC-DV-002890 (CAT I) — **Not_Applicable**

*Application web servers must be on a separate network segment from the application and database servers if it is a tiered application operating in the DoD DMZ.*

Not present in this artifact, per each rule's own scoping: no org-defined security attributes/data labeling (222393-5); no remote-access sessions INTO the application (222396-7 - the outbound model call is assessed at V-222596); no data-mining surface (222424); no tiered UI/storage architecture or web/database servers (222574, 222620, 222635, 222671); no XML processing in core and no web services (222593, 222608, 222625); no HTML/web UI, so no XSS/CSRF/hidden-field surface (222601, 222602, 222603 - the terminal-output analog, ANSI-escape injection, is affirmatively defended: picoagent/core/text.py strip_terminal_controls, tested); no SQL anywhere (222607); no mobile code (222618, 222665); no classified processing or production databases (222664, 222666, 265634); no HA/web-service redundancy requirement (222595). One caveat recorded for 222608: the stig-runner example plugin parses user-named CKL XML with stdlib xml.etree, which does not resolve external entities; internal-entity blowups are bounded by the bundled Expat >= 2.4 amplification limits.

**Evidence:** examples/plugins/stig-runner/ckl.py:41 (the only XML use, an opt-in example plugin); picoagent/core/text.py:520-535

### V-222642 / APSC-DV-003110 (CAT I) — **NotAFinding**

*The application must not contain embedded authentication data.*

CAT I. No embedded authentication data: every credential-shaped hit the APSC-DV-003110 probes returned was examined - all are documentation examples in plugin docstrings (es_doctor.py:29, grok_provider.py:9-12 show config-file syntax with placeholder values) or deliberately planted fake secrets in tests (which do not ship in the installed package, as the CI workflow itself documents). Real credentials flow only from the user's environment, the 0600 credentials store, or config files, never from code. Semgrep p/secrets and Trivy secret scanning run in CI as standing verification.

**Evidence:** probe APSC-DV-003110 (16 hits, all reviewed); examples/plugins/es-doctor/es_doctor.py:25-33 (docstring); examples/plugins/grok-provider/grok_provider.py:5-12 (docstring); .github/workflows/security-scan.yml:1-14

### V-222643 / APSC-DV-003120 (CAT I) — **Not_Applicable**

*The application must have the capability to mark sensitive/classified output when required.*

V-222641: the application implements no key-exchange protocol of its own; transport crypto is TLS via the platform ssl stack. V-222643: no classification guide applies and the tool is not designed for sensitive/classified output production; a deployment processing CUI or classified data would have to add marking capability, which does not exist. V-222668: host resource monitoring/alerting is an OS function; the tool itself fails loudly (raised errors) rather than degrading silently when resources run out.

**Evidence:** picoagent/core/provider.py (urllib/ssl only)

### V-222658 / APSC-DV-003240 (CAT I) — **Not_Reviewed**

*All products must be supported by the vendor or the development team.*

CAT I. Active maintenance is evident - commits within days of this assessment, an ongoing review cadence, and a stdlib-only dependency surface on Python >= 3.11 (a supported upstream) - but "supported by the vendor or development team" requires a support commitment an assessor can hold someone to, and no support statement, contact, or lifecycle policy exists in the repository. To close: obtain a written support commitment from the development team.

**Evidence:** git log (head commit 3645b9f, 2026-09); pyproject.toml (requires-python >=3.11, dependencies = [])

### V-222659 / APSC-DV-003250 (CAT I) — **NotAFinding**

*The application must be decommissioned when maintenance or support is no longer available.*

CAT I. The application is being actively maintained - the head commit is days old, preceded by a sustained cadence of feature and security-fix commits - and its only platform dependency (Python >= 3.11) is within upstream support. Nothing is unmaintained, so there is nothing to decommission. (The forward-looking support commitment question is V-222658, Not_Reviewed.)

**Evidence:** git log --oneline (3645b9f and predecessors, 2026-09); pyproject.toml

### V-222662 / APSC-DV-003280 (CAT I) — **Not_Applicable**

*Default passwords must be changed.*

picoagent has no user accounts of any kind: no account store, no creation/disable/removal functions, no default or built-in accounts, no passwords to default. It is a process the already-OS-authenticated user starts; identity and account management are wholly the operating system's. The account-lifecycle events this rule audits or constrains cannot occur in the application. (For V-222662: the probe for APSC-DV-003280 returned zero hits; no credentials of any kind ship with the product.)

**Evidence:** No account-management code exists anywhere under picoagent/ or examples/plugins/; probe APSC-DV-003280: 0 hits

---

## 3. CAT II and CAT III — full coverage

Opens are listed individually; rules sharing one determination and one reason are grouped.
Every rule below was individually checked against its V6R4 check content before being
placed in a group.

### 3.1 CAT II / CAT III Opens

#### V-222444 / APSC-DV-000650 (CAT II) — **Open**

*The application must not write sensitive data into the application logs.*

Sensitive data can reach the application log in the shipped default configuration. The session log records every tool result verbatim and replays it; the built-in shell tool passes the ENTIRE process environment to every command the model runs (demonstrated during this assessment: a planted FAKE_API_KEY was echoed by `echo $FAKE_API_KEY` through ShellTool and returned in the tool result, which the loop appends to the session file). The credential-guard plugin exists precisely to close this - its own docstring calls it "the actual leak path this plugin closes" - but it is an example plugin, not a default. What IS kept out of the log: keys typed at /secrets (slash commands bypass logging), keys echoed in provider error bodies (scrubbed). WHAT TO CHANGE: make the environment allowlist the built-in shell's default (or ship credential-guard enabled/required by default).

**Evidence:** picoagent/core/tools.py:398 ({**os.environ, "PICOAGENT": "1"}); verified by execution: built-in shell sees planted secret = True; picoagent/core/loop.py:243 (results appended to session); examples/plugins/credential-guard/credential_guard.py:20-28 (docstring naming this exact path)

#### V-222469 / APSC-DV-000940 (CAT II) — **Open**

*The application must log application shutdown events.*

Application shutdown is not logged. The session file simply stops at the last appended entry: the session_end event is emitted to plugins and frontends but never persisted, so the record cannot distinguish a clean exit from a crash after the last message. Marginal in impact (the single user initiated the shutdown) but the record the rule requires does not exist, and the fix is one appended entry. WHAT TO CHANGE: append a session-end entry (custom kind) from the session_end emit point in run_agent's finally block.

**Evidence:** picoagent/cli.py:368-369 (session_end emitted, nothing appended); picoagent/core/session.py (no shutdown entry kind)

#### V-222500 / APSC-DV-001280 (CAT II) — **Open**

*The application must protect audit information from any type of unauthorized read access.*

Audit information is not protected from unauthorized read access. The session log - the application's only activity record, containing the full conversation, every command executed and every tool result - is created with default umask permissions (verified by execution on this host: file 0644, directory 0755), so every local account on a multi-user machine can read it. The codebase demonstrably knows how to do better: the credentials file is 0600 from creation, the trust store is republished with mkstemp's owner-only mode, and spill files are 0600. WHAT TO CHANGE: create ~/.picoagent/sessions (and each session file) with owner-only permissions, as trust.json already is.

**Evidence:** picoagent/core/session.py:38,98-105 (mkdir/open with no mode narrowing); verified by execution: session file mode 0o644, dir 0o755 under umask 022; contrast picoagent/plugins/loader.py:688-691 and credential_guard.py:158

#### V-222513 / APSC-DV-001430 (CAT II) — **Open**

*The application must have the capability to prevent the installation of patches, service packs, or application components without verification the software component has been digitally signed using a certificate that is recognized and approved by the organization.*

The application has no capability to verify digital signatures on software components before installation. Plugins - code that runs with the user's full privileges - are installed by git clone from whatever host the spec names and gated by user review plus a SHA-256 fingerprint recorded AT approval time; nothing verifies a signature from a recognized certificate authority at any point, git tag/commit signatures are never checked, and pip-installed plugin python_deps are fetched without hash pinning. The trust-store fingerprint is a genuine integrity control for detecting change AFTER approval, but it attests only that the user clicked yes on those bytes, not that any recognized organization signed them. WHAT TO CHANGE: verify signed git tags/commits (or detached signatures over the fingerprint) against organization-approved keys at plugin add/upgrade time, and pass --require-hashes semantics to plugin dependency installs.

**Evidence:** picoagent/plugins/loader.py:246-262 (_clone_or_update: clone+checkout, no signature verification), 337-340 (install_deps: plain pip install), 385-395 (plugin_fingerprint: sha256 recorded at approval, not verified against any signer)

#### V-222587 / APSC-DV-002330 (CAT II) — **Open**

*The application must protect the confidentiality and integrity of stored information when required by DOD policy or the information owner.*

Stored information whose confidentiality the information owner would plainly require is left at default permissions - the same root defect as V-222500, recorded here because this rule covers the stored data itself rather than the audit trail: session logs hold repository content, tool output, and anything a command printed (including secrets, per V-222444), in plaintext files readable by every local account. The credentials store and trust records are properly owner-restricted, and config hardening exists but only inside the opt-in credential-guard plugin (harden_config_files). WHAT TO CHANGE: same fix as V-222500, plus run harden_config_files-equivalent logic in core so ~/.picoagent/config.toml (a documented api_key location) is owner-only without requiring a plugin.

**Evidence:** verified: session file 0o644; examples/plugins/credential-guard/credential_guard.py:430-441 (harden_config_files exists only in the opt-in plugin, and its own docstring records config.toml "readable by every account on the machine")

#### V-222649 / APSC-DV-003180 (CAT III) — **Open**

*Code coverage statistics must be maintained for each release of the application.*

CAT III. No code coverage statistics are maintained: no coverage tooling is configured in the repository or CI, and no coverage figures appear in the docs, for any release. WHAT TO CHANGE: add coverage measurement to the CI test step and record the statistic per release.

**Evidence:** .github/workflows/security-scan.yml (test step runs unittest with no coverage); no coverage config anywhere in the tree

#### V-222655 / APSC-DV-003230 (CAT II) — **Open**

*Threat models must be documented and reviewed for each application release and updated as required by design and functionality changes or when new threats are discovered.*

No threat model exists, and the repository says so itself: the security README lists "Threat model - assets, adversaries, and attack surface" under "Planned - Not yet written. Listed so the gaps are visible rather than implied." The trust-boundaries document is a strong foundation but is explicitly not the threat model. WHAT TO CHANGE: write the threat model against the existing boundaries document and review it each release, as the rule requires.

**Evidence:** docs/security/README.md:9-14 (verbatim self-declaration)

### 3.2 Grouped determinations

#### 20 rules — **Not_Applicable**

**Rules:** V-222387 (APSC-DV-000010), V-222388 (APSC-DV-000060), V-222389 (APSC-DV-000070), V-222390 (APSC-DV-000080), V-222391 (APSC-DV-000090), V-222392 (APSC-DV-000100, CAT III), V-222441 (APSC-DV-000620), V-222442 (APSC-DV-000630), V-222443 (APSC-DV-000640), V-222445 (APSC-DV-000660), V-222520 (APSC-DV-001520), V-222521 (APSC-DV-001530), V-222549 (APSC-DV-001800), V-222575 (APSC-DV-002210), V-222576 (APSC-DV-002220), V-222579 (APSC-DV-002250), V-222580 (APSC-DV-002260), V-222581 (APSC-DV-002270), V-222582 (APSC-DV-002280), V-222583 (APSC-DV-002290)

picoagent is a locally-run single-user CLI process. It performs no user logon, issues no session identifiers or cookies, and maintains no authenticated communication sessions; the "session" in picoagent is a local JSONL conversation log, not an authentication session. The events and artifacts this rule regulates do not exist in the application.

**Evidence:** picoagent/cli.py:721-730 (main: no auth path); picoagent/core/session.py:1-15 (session = append-only JSONL conversation log); no cookie/session-token code exists in the codebase (probe APSC-DV-000010/000060 hits are all docstrings/tests)

#### 18 rules — **Not_Applicable**

**Rules:** V-222393 (APSC-DV-000110), V-222394 (APSC-DV-000120), V-222395 (APSC-DV-000130), V-222396 (APSC-DV-000160), V-222397 (APSC-DV-000170), V-222424 (APSC-DV-000450), V-222574 (APSC-DV-002150), V-222593 (APSC-DV-002390), V-222595 (APSC-DV-002410), V-222603 (APSC-DV-002500), V-222618 (APSC-DV-002870), V-222625 (APSC-DV-002950), V-222635 (APSC-DV-003040), V-222664 (APSC-DV-003290), V-222665 (APSC-DV-003300), V-222666 (APSC-DV-003310), V-222671 (APSC-DV-003350), V-265634 (APSC-DV-002010)

Not present in this artifact, per each rule's own scoping: no org-defined security attributes/data labeling (222393-5); no remote-access sessions INTO the application (222396-7 - the outbound model call is assessed at V-222596); no data-mining surface (222424); no tiered UI/storage architecture or web/database servers (222574, 222620, 222635, 222671); no XML processing in core and no web services (222593, 222608, 222625); no HTML/web UI, so no XSS/CSRF/hidden-field surface (222601, 222602, 222603 - the terminal-output analog, ANSI-escape injection, is affirmatively defended: picoagent/core/text.py strip_terminal_controls, tested); no SQL anywhere (222607); no mobile code (222618, 222665); no classified processing or production databases (222664, 222666, 265634); no HA/web-service redundancy requirement (222595). One caveat recorded for 222608: the stig-runner example plugin parses user-named CKL XML with stdlib xml.etree, which does not resolve external entities; internal-entity blowups are bounded by the bundled Expat >= 2.4 amplification limits.

**Evidence:** examples/plugins/stig-runner/ckl.py:41 (the only XML use, an opt-in example plugin); picoagent/core/text.py:520-535

#### 6 rules — **Not_Applicable**

**Rules:** V-222398 (APSC-DV-000180), V-222401 (APSC-DV-000210), V-222402 (APSC-DV-000220), V-222405 (APSC-DV-000250), V-222406 (APSC-DV-000260), V-222573 (APSC-DV-002050)

The rule's own check content makes it NA when the application does not use SOAP, WS-Security or SAML. picoagent speaks only OpenAI-dialect JSON over HTTP(S) to a user-configured model endpoint; there is no SOAP, WS-Security, or SAML anywhere in the codebase.

**Evidence:** picoagent/core/provider.py:259-293 (JSON POST to {base_url}/chat/completions is the only protocol); grep for SOAP/SAML/WSS across the tree returns nothing outside the STIG rule text fixtures

#### 20 rules — **Not_Applicable**

**Rules:** V-222407 (APSC-DV-000280), V-222408 (APSC-DV-000290), V-222409 (APSC-DV-000300), V-222410 (APSC-DV-000310, CAT III), V-222411 (APSC-DV-000320, CAT III), V-222412 (APSC-DV-000330), V-222413 (APSC-DV-000340), V-222414 (APSC-DV-000350), V-222415 (APSC-DV-000360), V-222416 (APSC-DV-000370), V-222417 (APSC-DV-000380, CAT III), V-222418 (APSC-DV-000390, CAT III), V-222419 (APSC-DV-000400, CAT III), V-222420 (APSC-DV-000410, CAT III), V-222421 (APSC-DV-000420), V-222422 (APSC-DV-000430, CAT III), V-222433 (APSC-DV-000540), V-222467 (APSC-DV-000880), V-222619 (APSC-DV-002880), V-222661 (APSC-DV-003270)

picoagent has no user accounts of any kind: no account store, no creation/disable/removal functions, no default or built-in accounts, no passwords to default. It is a process the already-OS-authenticated user starts; identity and account management are wholly the operating system's. The account-lifecycle events this rule audits or constrains cannot occur in the application. (For V-222662: the probe for APSC-DV-003280 returned zero hits; no credentials of any kind ship with the product.)

**Evidence:** No account-management code exists anywhere under picoagent/ or examples/plugins/; probe APSC-DV-003280: 0 hits

#### V-222423 / APSC-DV-000440 (CAT II) — **NotAFinding**

*Application data protection requirements must be identified and documented.*

Data protection requirements are identified and documented: the trust-boundaries document defines what is trusted at which level, where each credential may live and travel, which paths are blocked and by what, and states its limits explicitly.

**Evidence:** docs/security/trust-boundaries.md (596 lines: boundaries, credential travel, known limits); docs/security/README.md (standing constraints)

#### 5 rules — **Not_Applicable**

**Rules:** V-222426 (APSC-DV-000470), V-222427 (APSC-DV-000480), V-222428 (APSC-DV-000490), V-222429 (APSC-DV-000500), V-222431 (APSC-DV-000520)

Access-control and privileged-function rules presuppose application-managed subjects, objects, roles, or privilege tiers. picoagent manages none: every action it takes runs strictly as the one invoking OS user, and the operating system enforces authorization on each file and process access. There is no non-privileged/privileged distinction inside the application to enforce or audit. The one genuinely novel principal - the model - is not a user; the controls the design offers over it (tool_call guard events, confine_to_project, api.set_active_tools, permission-gate) are assessed under V-222604, where the default-configuration gap is recorded as Open.

**Evidence:** docs/security/trust-boundaries.md:387-403 (confinement model); picoagent/core/tools.py:153-176 (resolve_path contract)

#### 4 rules — **Not_Applicable**

**Rules:** V-222434 (APSC-DV-000550, CAT III), V-222435 (APSC-DV-000560, CAT III), V-222436 (APSC-DV-000570, CAT III), V-222437 (APSC-DV-000580, CAT III)

The Standard Mandatory DoD Notice and Consent Banner and last-logon display are required at application logon. picoagent has no logon: it grants nothing the OS shell has not already granted. The banner obligation sits with the host's logon path (OS STIG), not with a locally spawned CLI process.

**Evidence:** No authentication or access-granting boundary exists in picoagent/cli.py

#### 24 rules — **Not_Applicable**

**Rules:** V-222438 (APSC-DV-000590), V-222439 (APSC-DV-000600), V-222447 (APSC-DV-000680), V-222448 (APSC-DV-000690), V-222449 (APSC-DV-000700), V-222450 (APSC-DV-000710), V-222451 (APSC-DV-000720), V-222452 (APSC-DV-000730), V-222453 (APSC-DV-000740), V-222454 (APSC-DV-000750), V-222455 (APSC-DV-000760), V-222456 (APSC-DV-000770), V-222457 (APSC-DV-000780), V-222458 (APSC-DV-000790), V-222459 (APSC-DV-000800), V-222460 (APSC-DV-000810), V-222461 (APSC-DV-000820), V-222462 (APSC-DV-000830), V-222463 (APSC-DV-000840), V-222466 (APSC-DV-000870), V-222470 (APSC-DV-000950), V-222475 (APSC-DV-001000), V-222477 (APSC-DV-001020), V-222672 (APSC-DV-003360, CAT III)

These rules require audit records for events that structurally cannot occur in picoagent: logons, account and privilege changes, security levels/objects/categories, HTTP requests served, connecting client IPs, multi-user attribution, concurrent logons from different workstations, and multi-component record aggregation. The application has one human principal (the invoking OS user), serves no requests, and manages no privileges or security attributes. Where an analogous event DOES exist (tool execution, file access by the agent), it is recorded - see the NotAFinding determinations on V-222446/222465/222468/222471/222472/222478.

**Evidence:** picoagent/core/session.py (the only persistent record); picoagent/core/loop.py:303-338 (every tool call and result is appended to it)

#### 13 rules — **NotAFinding**

**Rules:** V-222446 (APSC-DV-000670), V-222464 (APSC-DV-000850), V-222465 (APSC-DV-000860), V-222468 (APSC-DV-000910), V-222471 (APSC-DV-000960), V-222472 (APSC-DV-000970), V-222473 (APSC-DV-000980), V-222474 (APSC-DV-000990), V-222476 (APSC-DV-001010), V-222478 (APSC-DV-001030), V-222497 (APSC-DV-001250), V-222498 (APSC-DV-001260), V-222499 (APSC-DV-001270)

Assessed against intent (the literal check assumes a multi-user server): picoagent's session log is its activity/audit record, and it satisfies what this rule asks of the events that exist. Auditing starts at startup (the session file is created with a header before the first prompt); every message entry carries a float epoch timestamp (system-clock, UTC-mappable, sub-second granularity); every tool call is recorded with its full argument text and its result including error/success (ToolResult.is_error), so accesses and changes to data by the agent - and the full text of every command it ran - are on disk verbatim; entry kinds and tool names identify the component that produced each record.

**Evidence:** picoagent/core/types.py:53 (ts: float = field(default_factory=time.time)); picoagent/core/session.py:41-47 (header written at creation, "created": time.time()); picoagent/core/loop.py:242-243,281 (assistant messages and tool results appended); verified by execution: message entries carry role/text/tool_calls/tool_results/ts

#### 24 rules — **Not_Applicable**

**Rules:** V-222479 (APSC-DV-001040), V-222480 (APSC-DV-001050), V-222481 (APSC-DV-001070), V-222482 (APSC-DV-001080), V-222483 (APSC-DV-001090), V-222484 (APSC-DV-001100), V-222485 (APSC-DV-001110), V-222487 (APSC-DV-001130), V-222488 (APSC-DV-001140), V-222489 (APSC-DV-001150), V-222490 (APSC-DV-001160), V-222491 (APSC-DV-001170), V-222492 (APSC-DV-001180), V-222493 (APSC-DV-001190), V-222494 (APSC-DV-001200), V-222495 (APSC-DV-001210), V-222496 (APSC-DV-001220), V-222503 (APSC-DV-001310), V-222504 (APSC-DV-001320), V-222505 (APSC-DV-001330), V-222506 (APSC-DV-001340), V-222507 (APSC-DV-001350), V-222508 (APSC-DV-001360), V-222509 (APSC-DV-001370)

These rules require enterprise audit infrastructure: centralized management and repositories, off-loading, SA/ISSO capacity and failure alerting, audit reduction/report-generation tooling, separately protected audit tools, seven-day audit backups, cryptographic audit signing, transaction recovery logs. picoagent is a local single-user CLI whose records are ordinary files under the invoking user's home directory; there are no SA/ISSO roles inside the tool, no audit toolchain ships with it, and record retention/backup/centralization are properties of the host and organization, not of this process. (V-222479: the application is not transaction-based; no database.) Note: protection of the records themselves IS assessable and is Open - see V-222500.

**Evidence:** picoagent/core/session.py (plain JSONL files under ~/.picoagent/sessions/)

#### V-222486 / APSC-DV-001120 (CAT II) — **NotAFinding**

*The application must shut down by default upon audit failure (unless availability is an overriding concern).*

Intent: shut down rather than continue unaudited on audit failure. An append to the session log that fails (disk full, permissions) raises OSError out of Session._write/append_message; nothing in the loop catches it, so the run dies rather than continuing with recording silently off. Determined by code reading, not by fault injection - stated as such.

**Evidence:** picoagent/core/session.py:98-105 (_write opens and writes with no catch); picoagent/core/loop.py:221,243,281 (append_message called outside any try)

#### 2 rules — **NotAFinding**

**Rules:** V-222501 (APSC-DV-001290), V-222502 (APSC-DV-001300)

Audit information (the session log) is protected from unauthorized modification and deletion by standard OS discretionary controls: files are 0644 and the containing directories 0755 under the default umask (verified by execution), so only the owning user can write to or unlink them. Owner-level processes can of course modify their own files - the same trust model as every file the user owns. (Unauthorized READ is the defect; that is V-222500.)

**Evidence:** verified by execution: session file 0o644 / dir 0o755 - group/other have no write bit

#### V-222510 / APSC-DV-001390 (CAT II) — **NotAFinding**

*The application must prohibit user installation of software without explicit privileged status.*

Software installation into the application is gated on explicit privileged (user) action: no plugin loads until the user has reviewed its manifest and approved it, the approval is fingerprinted over every file, and changed code is refused until re-approved. A repository (the untrusted layer) cannot install into or move the user's plugin checkouts.

**Evidence:** picoagent/plugins/loader.py:1030-1046 (trust gate), 428-520 (TrustStore), 293-316 (PluginOwnershipError off-limits checks); picoagent/cli.py:622-659 (trust_command consent flow); tests/test_plugin_add_consent.py

#### V-222511 / APSC-DV-001410 (CAT II) — **NotAFinding**

*The application must enforce access restrictions associated with changes to application configuration.*

Access restrictions on configuration changes hold in both senses assessable here: config files are writable only by the owning user under default permissions, and the untrusted configuration layer (a cloned repository's .picoagent/config.toml) is structurally prevented from changing security-relevant settings - USER_ONLY keys are stripped with a user-visible notice, and per-plugin tables are layered so a repository value is only read where a plugin explicitly names it.

**Evidence:** picoagent/core/config.py:63-78 (USER_ONLY), 176-299 (PluginConfig layering), 321-342 (_strip_user_only); tests/test_config_refusals.py, tests/test_plugin_config_provenance.py

#### V-222512 / APSC-DV-001420 (CAT II) — **Not_Applicable**

*The application must audit who makes configuration changes to the application.*

Configuration is plain files owned and edited by the single user; the application has no configuration interface of its own to audit, and file-change auditing on the host is the OS baseline's. The change the application CAN observe - a repository config asking for something it may not have - is reported to the user at session start rather than silently applied.

**Evidence:** picoagent/cli.py:300-338 (warn_about_ignored_project_keys / unreadable config notices)

#### V-222514 / APSC-DV-001440 (CAT II) — **Not_Applicable**

*The applications must limit privileges to change the software resident within software libraries.*

The application ships no shared software libraries whose modification it could restrict beyond OS file permissions: the package is installed per-user (venv) and plugins live in user-owned directories. The application-level control that does exist - refusal to LOAD modified plugin code until re-approved, and the ownership rule that keeps a repository from writing into the user's plugin checkouts - is assessed at V-222510/V-222513.

**Evidence:** picoagent/plugins/loader.py:293-316 (checkout ownership), 1030-1046 (changed code refused)

#### V-222515 / APSC-DV-001460 (CAT II) — **NotAFinding**

*An application vulnerability assessment must be conducted.*

A vulnerability assessment is conducted continuously: CI runs Bandit, Semgrep (p/python + p/secrets) and Trivy (secret, misconfig, vuln) on every push against the shipped package plus example plugins, with explicit guards against empty-corpus scans and redaction of scan artifacts. (Caveat: the latest run's results are on the CI system, not in the repository.)

**Evidence:** .github/workflows/security-scan.yml (whole file); probe APSC-DV-001460: "security tooling named: bandit, dependabot, semgrep, trivy" + sonarcloud.yml

#### 24 rules — **Not_Reviewed**

**Rules:** V-222516 (APSC-DV-001480), V-222519 (APSC-DV-001510), V-222621 (APSC-DV-002900), V-222622 (APSC-DV-002910), V-222623 (APSC-DV-002920), V-222627 (APSC-DV-002970), V-222628 (APSC-DV-002980), V-222629 (APSC-DV-002990), V-222630 (APSC-DV-002995), V-222631 (APSC-DV-003000), V-222632 (APSC-DV-003010), V-222633 (APSC-DV-003020), V-222634 (APSC-DV-003030), V-222636 (APSC-DV-003050), V-222637 (APSC-DV-003060), V-222638 (APSC-DV-003070), V-222640 (APSC-DV-003090), V-222645 (APSC-DV-003140), V-222646 (APSC-DV-003150), V-222651 (APSC-DV-003200), V-222657 (APSC-DV-003236), V-222660 (APSC-DV-003260, CAT III), V-222669 (APSC-DV-003340, CAT III), V-222673 (APSC-DV-003400)

Organization/process/deployment requirement that cannot be determined from a repository: usage-restriction policies, PPSM CAL registration, ISSO audit-retention/review/reporting duties, third-party configuration guidance, CM-repository patching and access reviews, SCM plan and CCB, DoD IPv6 Standards Profile compliance of the environment, contingency planning and backup procedures, the deployment-time hash-validation process, a designated security tester, accreditation-impact assessment, an incident response plan, decommission-notification procedures, update-notification registration, and annual security training. To close each: obtain the named organizational artifact or interview the responsible role. Notes where the repo does carry partial signal: the plugin trust store is a working sha256 validation mechanism (loader.py:plugin_fingerprint) relevant to 222645; the security docs README does not list an incident response plan even among planned documents (222657).

**Evidence:** docs/security/README.md:9-20 (planned-documents list)

#### V-222517 / APSC-DV-001490 (CAT II) — **Not_Applicable**

*The application must employ a deny-all, permit-by-exception (whitelist) policy to allow the execution of authorized software programs.*

Per the check's own scoping sentence: "If the application is not a configuration management or similar type of application designed to manage system processes and configurations, this requirement is not applicable." picoagent is a coding agent, not a CM/software-distribution system. Recorded with a cross-reference rather than silently: the absence of any allowlist over what the MODEL may execute is part of the V-222604 CAT I finding.

**Evidence:** rule check content (first sentence); see V-222604

#### V-222518 / APSC-DV-001500 (CAT II) — **NotAFinding**

*The application must be configured to disable non-essential capabilities.*

Non-essential capabilities are disabled by construction: the core ships only the model loop, four tools, skills, config and a session log; everything else (MCP, subagents, permission modes, providers, admin tooling) is a plugin that does not exist in the process until the user installs, reviews and trusts it.

**Evidence:** docs/architecture.md ("Why the core is this small"); picoagent/core/tools.py:415 (BUILTIN_TOOLS = read/write/edit/shell); plugin trust gate as cited at V-222510

#### 30 rules — **Not_Applicable**

**Rules:** V-222523 (APSC-DV-001550), V-222524 (APSC-DV-001560), V-222525 (APSC-DV-001570), V-222526 (APSC-DV-001580), V-222527 (APSC-DV-001590), V-222528 (APSC-DV-001600), V-222529 (APSC-DV-001610), V-222530 (APSC-DV-001620), V-222531 (APSC-DV-001630), V-222532 (APSC-DV-001640), V-222533 (APSC-DV-001650), V-222534 (APSC-DV-001660), V-222535 (APSC-DV-001670), V-222537 (APSC-DV-001690), V-222538 (APSC-DV-001700), V-222539 (APSC-DV-001710), V-222540 (APSC-DV-001720), V-222541 (APSC-DV-001730), V-222544 (APSC-DV-001760), V-222545 (APSC-DV-001770), V-222546 (APSC-DV-001780), V-222547 (APSC-DV-001790), V-222548 (APSC-DV-001795), V-222552 (APSC-DV-001830), V-222553 (APSC-DV-001840), V-222556 (APSC-DV-001870), V-222557 (APSC-DV-001880), V-222558 (APSC-DV-001890), V-222559 (APSC-DV-001900), V-222560 (APSC-DV-001910)

picoagent authenticates no users. It exposes no interface to any second person: it is a foreground process bound to one terminal, run under an OS identity the platform already authenticated. Most rules in this family carry their own NA carve-out ("If the application does not use passwords, this requirement is Not Applicable"). The only credential in the system is an API key the application presents TO a remote model endpoint; it is not a user password, is entered via getpass (never echoed, never logged - see V-222554), stored owner-only (0600/icacls), and transmitted as an Authorization header. Deployments must still configure an https base_url so that key never crosses the wire in clear; see the V-222596 caveat.

**Evidence:** docs/security/trust-boundaries.md:318-366 (key lifecycle); examples/plugins/credential-guard/credential_guard.py:125-163 (0600/icacls storage)

#### 6 rules — **Not_Applicable**

**Rules:** V-222561 (APSC-DV-001930), V-222562 (APSC-DV-001940), V-222563 (APSC-DV-001950), V-222564 (APSC-DV-001960), V-222565 (APSC-DV-001970), V-222566 (APSC-DV-001980)

picoagent provides no maintenance or diagnostic sessions to remote maintainers; it is not remotely reachable at all (no listening sockets anywhere in the codebase - the only network I/O is an outbound client request to the configured model endpoint).

**Evidence:** picoagent/core/provider.py (urllib client only); no socket.listen/bind or server code exists in the tree

#### V-222567 / APSC-DV-001995 (CAT II) — **NotAFinding**

*The application must not be vulnerable to race conditions.*

Race-condition defenses are present at the shared-state seams: per-file asyncio locks serialize concurrent write/edit tool calls on the same path (keyed by resolved path so symlink aliases share a lock); the trust store is written to a temp file, fsynced, and atomically renamed so readers see all of a write or none; read-modify-write on the credentials store holds a threading lock. Determined by code reading and the unit tests that pin these behaviors.

**Evidence:** picoagent/core/tools.py:179-188 (file_lock), 240,263 (async with file_lock); picoagent/plugins/loader.py:663-694 (_save: mkstemp+fsync+os.replace); examples/plugins/credential-guard/credential_guard.py:50,108 (_WRITE_LOCK); tests/test_torn_state_files.py

#### V-222568 / APSC-DV-002000 (CAT II) — **NotAFinding**

*The application must terminate all network connections associated with a communications session at the end of the session.*

Network connections are per-request and closed at the end of each exchange: both the streaming completion read and the model listing open the connection in a context manager, and a refused redirect closes the live response explicitly rather than leaking the socket.

**Evidence:** picoagent/core/provider.py:373 (with _OPENER.open(request, timeout=600)), 347, 203-216 (redirect refusal closes fp)

#### V-222570 / APSC-DV-002020 (CAT II) — **Not_Applicable**

*The application must utilize FIPS-validated cryptographic modules when signing application components.*

The application signs no components: no code signing, no release signing, no signature generation exists in the tree (that verification is also absent is the V-222513 finding; this rule governs the crypto module used WHEN signing, which never happens).

**Evidence:** grep: no signing code in picoagent/ or examples/

#### 3 rules — **Not_Reviewed**

**Rules:** V-222571 (APSC-DV-002030), V-222572 (APSC-DV-002040), V-222584 (APSC-DV-002300)

Host-inherited cryptography: picoagent uses only the platform Python's hashlib (SHA-256 exclusively - a FIPS-approved algorithm) and the platform ssl/OpenSSL stack with the operating system trust store for TLS. Whether those modules are FIPS-validated, and whether only DoD-approved CAs anchor the trust store, are properties of the host build and its configuration, not of this codebase. To close: verify the deployed Python links a FIPS-validated crypto module and the OS trust store carries DoD roots.

**Evidence:** picoagent/plugins/loader.py:390-404 (hashlib.sha256 only); picoagent/core/provider.py (urllib -> ssl default context, which validates certificates against the system store)

#### V-222586 / APSC-DV-002320 (CAT II) — **NotAFinding**

*In the event of a system failure, applications must preserve any information necessary to determine cause of failure and any information necessary to return to operations with least disruption to mission processes.*

Failure information is preserved: the session log survives crashes (append-only; a torn final line is detected, reported with the byte count lost, and dropped so the file stays resumable), exceptions are logged with tracebacks via log.exception, distinct exit codes separate required-plugin refusal / provenance failure / model unreachable, and provider errors are recorded on the runtime so headless callers get a verdict.

**Evidence:** picoagent/core/session.py:50-96; picoagent/core/loop.py:148-150,358-363; picoagent/cli.py:47-56,343-370

#### V-222590 / APSC-DV-002360 (CAT II) — **Not_Applicable**

*The application must isolate security functions from non-security functions.*

Security-function isolation presupposes differing privilege domains inside the application. By explicit, documented design every line of code that runs in-process - core and approved plugins alike - runs in a single trust domain with the user's own privileges; the enforced boundary is the load-time trust decision, not runtime containment, and the documentation states this rather than overselling a sandbox. There is no lower-privilege domain from which to isolate security functions.

**Evidence:** docs/security/trust-boundaries.md:44-46 ("There is no sandbox... the boundary is the trust decision at load time"); docs/security/README.md ("Plugin code runs with the user's privileges")

#### V-222591 / APSC-DV-002370 (CAT II) — **NotAFinding**

*The application must maintain a separate execution domain for each executing process.*

Separate execution domains are inherited from the OS process model and reinforced: every spawned command runs in its own process (own address space) and is additionally made leader of its own process group/session so it can be signalled as a tree without touching picoagent's own group.

**Evidence:** picoagent/core/tools.py:280-312 (own_process_group: start_new_session=True / CREATE_NEW_PROCESS_GROUP)

#### V-222592 / APSC-DV-002380 (CAT II) — **NotAFinding**

*Applications must prevent unauthorized and unintended information transfer via shared system resources.*

Shared-resource leakage is controlled at the one shared surface the tool uses: spilled tool output goes to NamedTemporaryFile, which creates 0600 files (verified by execution on this host: mode 0o600); the trust store is republished with mkstemp's owner-only mode. (The session log's own permissions are a separate defect - see V-222587.)

**Evidence:** picoagent/core/tools.py:84-88 (spill_to_tempfile); verified: spill file mode 0o600; picoagent/plugins/loader.py:688-691

#### V-222594 / APSC-DV-002400 (CAT II) — **NotAFinding**

*The application must restrict the ability to launch Denial of Service (DoS) attacks against itself or other information systems.*

The application bounds its own resource consumption: tool output is truncated (50KB/2000 lines) with full text spilled to disk instead of the context window, shell commands run under a timeout and are killed as whole process trees (SIGTERM grace then SIGKILL, bounded reap), repository probes cap file size, file count and per-hit excerpts, and provider reads carry timeouts. The model's general ability to run resource-consuming commands through the shell is the V-222604 finding, not a separate one here.

**Evidence:** picoagent/core/tools.py:66-88,396-412 (truncate/timeout), 342-376 (kill_process_tree); examples/plugins/stig-runner/evidence.py:44-48 (MAX_FILE_BYTES/MAX_FILES caps); picoagent/core/provider.py:347,373 (timeouts)

#### 3 rules — **NotAFinding**

**Rules:** V-222597 (APSC-DV-002450), V-222598 (APSC-DV-002460), V-222599 (APSC-DV-002470)

Transmitted information (the conversation, tool results, and the API key) is protected in the shipped default configuration: the default endpoint is https://api.openai.com/v1 with certificate validation on (platform ssl default), non-HTTP(S) schemes are refused so file:/ftp:/data: can never be an endpoint, and redirects are followed only within the exact origin (scheme+host+port) - a cross-origin or https->http redirect is refused outright rather than followed with headers, a stronger stance than requests/curl, demonstrated against a live second server per the docs. Preparation and reception happen in process memory feeding directly into that channel. CAVEAT for the deployer, stated because the check is configuration-dependent: http:// endpoints are deliberately permitted (local Ollama/vLLM is the headline use), and nothing warns when an http URL points at a NON-loopback host - a deployment must configure https for any remote endpoint, and a warning for remote-http would be a worthwhile hardening.

**Evidence:** picoagent/core/provider.py:128-156 (HTTP_SCHEMES/check_base_url), 159-239 (_SameOriginRedirects, _OPENER), 270-271 (https default); docs/security/trust-boundaries.md:365-381; tests/test_plugin_redirects.py

#### V-222600 / APSC-DV-002480 (CAT II) — **NotAFinding**

*The application must not disclose unnecessary information to users.*

Unnecessary disclosure is affirmatively suppressed: the API key is scrubbed from provider error bodies before they reach the terminal or --json stream, failure reports are bounded at 2000 chars, evidence excerpts mask credential-shaped values to 4 chars, and /secrets shows only a last-4 mask.

**Evidence:** picoagent/core/provider.py:357-367; picoagent/core/text.py:495-547; examples/plugins/stig-runner/evidence.py:79-84,229-244 (mask_secrets)

#### V-222605 / APSC-DV-002520 (CAT II) — **NotAFinding**

*The application must protect from canonical representation vulnerabilities.*

Canonicalization is handled at a single seam both tools and guards must share: every model-supplied path is @-stripped, ~-expanded, resolved against the session directory and symlink-resolved before any decision is made about it, precisely because guards that resolved paths themselves drifted and were bypassed (documented, with the historical bypasses named). Probe containment resolves every candidate and refuses symlinks leaving the root; the ES gate refuses %2f and .. before consulting its allowlist.

**Evidence:** picoagent/core/tools.py:109-150 (resolve_tool_path, with bypass history); examples/plugins/credential-guard/credential_guard.py:260-276,251-257 (inode identity); examples/plugins/stig-runner/evidence.py:139-166; docs/security/trust-boundaries.md:296-300; tests/test_gate_path_bypasses.py

#### V-222606 / APSC-DV-002530 (CAT II) — **NotAFinding**

*The application must validate all input.*

Input validation is systematic at each untrusted boundary: model tool calls are checked for required arguments against the tool's own schema before execution; repository config is parsed behind a catch broad enough for attacker-chosen bytes, with values shape-checked against plugin-declared defaults before use; manifests fail as one typed error; session resume validates entry structure; strings from any untrusted source are stripped of terminal controls and encoding-guarded before display.

**Evidence:** picoagent/core/loop.py:346-354 (missing_required_args); picoagent/core/config.py:139-173 (has_shape), 401-436 (_read_toml); picoagent/plugins/manifest.py:103-155; picoagent/core/text.py; tests/test_unreadable_manifest.py, tests/test_untrusted_text.py

#### V-222610 / APSC-DV-002570 (CAT II) — **NotAFinding**

*The application must generate error messages that provide information necessary for corrective actions without revealing information that could be exploited by adversaries.*

Error messages provide corrective information without handing an adversary anything beyond what the single local operator already has: provider errors are scrubbed of the API key and bounded, hostile exception text is neutralized and truncated at 2000 chars, refusals name the file/pattern/setting to fix (by design), and tracebacks go to the local stderr of the user who owns the process - there is no remote or less-privileged audience to protect.

**Evidence:** picoagent/core/provider.py:357-367; picoagent/core/text.py:495-547,577-610; picoagent/core/loop.py:148-150

#### V-222611 / APSC-DV-002580 (CAT II) — **Not_Applicable**

*The application must reveal error messages only to the ISSO, ISSM, or SA.*

There is only one audience: the invoking user is simultaneously the application's operator, administrator and sole user, and errors appear on their own terminal. The rule's separation of error detail (to ISSO/ISSM/SA) from generic messages (to users) presupposes distinct user classes that do not exist here.

**Evidence:** picoagent/frontends/ (all output paths are the local terminal or the local --json stream)

#### V-222613 / APSC-DV-002610 (CAT II) — **NotAFinding**

*The application must remove organization-defined software components after updated versions have been installed.*

Updated components replace rather than accumulate: a plugin lives in exactly one checkout directory per source, upgrades move that checkout in place (fetch + checkout + --ff-only merge), and no versioned side-by-side copies are created.

**Evidence:** picoagent/plugins/loader.py:246-262 (_clone_or_update), 320-334 (fast_forward)

#### V-222614 / APSC-DV-002630 (CAT II) — **NotAFinding**

*Security-relevant software updates and patches must be kept up to date.*

There are no third-party dependencies to patch: the runtime dependency surface is the Python standard library alone (dependencies = [], verified and recorded as evidence by the CI pipeline itself). Security-relevant patching therefore reduces to the host Python, which is outside this artifact. Plugin python_deps are per-plugin, opt-in, shown at the consent prompt, and checkable via picoagent upgrade.

**Evidence:** pyproject.toml (dependencies = []); .github/workflows/security-scan.yml ("Record the dependency surface" step); picoagent/plugins/upgrade.py

#### 3 rules — **Not_Applicable**

**Rules:** V-222615 (APSC-DV-002760), V-222616 (APSC-DV-002770), V-222617 (APSC-DV-002780, CAT III)

Per the check's own carve-out ("If the application is not designed or intended to perform security function testing, the requirement is not applicable"): picoagent is not designed as a security-function-verification system. Noted in its favour, without claiming the rule is met by it: the loader does re-verify the SHA-256 fingerprint of every trusted plugin at every startup, refuses changed code, and stops the session outright when a required security plugin cannot run, marking the notice urgent on stderr and as a plugin_skipped event.

**Evidence:** picoagent/plugins/loader.py:1030-1046 (trust re-check each load), 1194-1215 (required-plugin stops); picoagent/cli.py:262-277 (urgent "!!" reporting)

#### V-222624 / APSC-DV-002930 (CAT II) — **NotAFinding**

*The ISSO must ensure active vulnerability testing is performed.*

Active vulnerability testing is performed: three scanners run on every push (Bandit, Semgrep with p/python+p/secrets, Trivy), plus a 972-test suite that includes adversarial cases (hostile config bytes, torn files, path bypasses, redirect exfiltration). Process caveat: this evidences the practice, not the ISSO's ongoing ownership of it.

**Evidence:** .github/workflows/security-scan.yml; tests/test_gate_path_bypasses.py, tests/test_plugin_redirects.py, tests/test_untrusted_text.py

#### V-222626 / APSC-DV-002960 (CAT II) — **Not_Applicable**

*The designer must ensure the application does not store configuration and control files in the same directory as user data.*

The rule targets applications that store user data on served directories alongside configuration. picoagent stores its own state under ~/.picoagent (config, sessions, plugins, trust) and operates on whatever repository the user points it at; it does not serve or manage "user data" of its own. The one deliberate co-location - .picoagent/config.toml inside a project repository - is a documented design whose trust is bounded (USER_ONLY, plugin-table layering) precisely because it travels with content.

**Evidence:** picoagent/core/config.py:1-25,63-78

#### V-222639 / APSC-DV-003080 (CAT II) — **NotAFinding**

*Back-up copies of the application software or source code must be stored in a fire-rated container or stored separately (offsite).*

Development is in-house and the source is backed up offsite from the development environment: the repository's origin is a remote hosting service, so a full content-addressed copy exists off the development machine.

**Evidence:** git remote -v: origin https://github.com/opscontinuum/picoagent.git

#### 2 rules — **Not_Applicable**

**Rules:** V-222641 (APSC-DV-003100), V-222668 (APSC-DV-003330)

V-222641: the application implements no key-exchange protocol of its own; transport crypto is TLS via the platform ssl stack. V-222643: no classification guide applies and the tool is not designed for sensitive/classified output production; a deployment processing CUI or classified data would have to add marking capability, which does not exist. V-222668: host resource monitoring/alerting is an OS function; the tool itself fails loudly (raised errors) rather than degrading silently when resources run out.

**Evidence:** picoagent/core/provider.py (urllib/ssl only)

#### V-222644 / APSC-DV-003130 (CAT III) — **NotAFinding**

*Prior to each release of the application, updates to system, or applying patches; tests plans and procedures must be created and executed.*

Test plans and procedures exist and are executed: a 972-test suite (verified passing during this assessment), run as the gate in CI on every push, with the testing conventions documented.

**Evidence:** python3 -m unittest discover -s tests: "Ran 972 tests ... OK (skipped=5)"; docs/testing-and-conventions.md; .github/workflows/security-scan.yml ("The suite is the gate")

#### V-222647 / APSC-DV-003160 (CAT III) — **NotAFinding**

*Test procedures must be created and at least annually executed to ensure system initialization, shutdown, and aborts are configured to verify the system remains in a secure state.*

Secure-state behavior on initialization failure, damage and aborts is covered by executable test procedures run at least per-push: torn trust stores and session files, unreadable configs, refused required plugins, and startup refusal paths all have dedicated tests.

**Evidence:** tests/test_torn_state_files.py, tests/test_config_refusals.py, tests/test_startup_notices.py, tests/test_plugin_untrust.py

#### V-222648 / APSC-DV-003170 (CAT II) — **NotAFinding**

*An application code review must be performed on the application.*

Code review is performed and its outputs are traceable: the commit history documents multiple structured review rounds (architecture reviews, adversarial verification tiers) with findings closed by named commits, plus SAST tooling in CI. Caveat: no CODEOWNERS/CONTRIBUTING gate is present, so review is evidenced by history rather than enforced by repository configuration.

**Evidence:** git log: 7095cd2, 8a5d9fe ("Close the four MEDIUM findings from the adversarial reviews"), c678664, f684b78, b3f1d0a ("after an adversary broke two of the first attempts"); .github/workflows/security-scan.yml

#### V-222650 / APSC-DV-003190 (CAT II) — **NotAFinding**

*Flaws found during a code review must be tracked in a defect tracking system.*

Flaws found during review are tracked in a defect tracking system: review findings are closed through numbered pull requests/issues on the hosting service, referenced from the commits that fix them.

**Evidence:** git log: "(#13)", "(#12)", "(#24)" issue/PR references on finding-closure commits

#### V-222652 / APSC-DV-003210 (CAT II) — **NotAFinding**

*Security flaws must be fixed or addressed in the project plan.*

Security flaws are fixed rather than deferred: the history shows high and medium review findings driven to closure in dedicated commits, including re-fixes where adversarial re-testing broke first attempts.

**Evidence:** git log: f684b78 ("Close the three high findings..."), c678664 ("Close the five medium findings..."), b3f1d0a ("Repair three fixes an adversary broke")

#### V-222653 / APSC-DV-003215 (CAT III) — **NotAFinding**

*The application development team must follow a set of coding standards.*

A coding standard is followed: documented conventions plus ruff linting in use on the tree.

**Evidence:** docs/testing-and-conventions.md; .ruff_cache/ present at repo root

#### V-222654 / APSC-DV-003220 (CAT III) — **NotAFinding**

*The designer must create and update the Design Document for each release of the application.*

Design documentation exists and is current: architecture overview plus per-subsystem engineering docs (system overview, request lifecycle, data model, plugin lifecycle, 1.0 plan), updated in recent commits.

**Evidence:** docs/architecture.md; docs/engineering/{system-overview,request-lifecycle,data-model,plugin-lifecycle,1.0-plan}.md; commit 7095cd2, 3ed86f3

#### V-222656 / APSC-DV-003235 (CAT II) — **NotAFinding**

*The application must not be subject to error handling vulnerabilities.*

Error handling is a designed subsystem rather than an afterthought: tools never raise for expected failures (typed error results the model can act on), every plugin call-in (events, commands, tools) is caught so one failure cannot kill the session, exception rendering itself is hardened against hostile __str__/__format__/__name__, and the log formatter sanitizes tracebacks. The check's scan requirement is covered by the per-push SAST pipeline.

**Evidence:** picoagent/core/tools.py:5-9 (never-raise contract); picoagent/core/events.py:269-279; picoagent/core/loop.py:117-150,340-363; picoagent/core/text.py:577-610; picoagent/cli.py:690-718 (SafeLogFormatter)

#### V-222663 / APSC-DV-003285 (CAT II) — **NotAFinding**

*An Application Configuration Guide must be created and included with the application.*

A configuration guide exists and is unusually explicit about security-relevant settings: install and first-run (getting-started), every file the tool reads on its own initiative (README "What files picoagent reads"), all config keys with their trust implications (config.py docstrings + trust-boundaries), and plugin authoring/trust flows. Caveat: the docs themselves list "deployment guidance" for accredited environments as planned-but-unwritten.

**Evidence:** README.md:56+ ("What files picoagent reads"); docs/getting-started.md; docs/security/trust-boundaries.md; docs/security/README.md:9-20

#### V-222667 / APSC-DV-003320 (CAT II) — **NotAFinding**

*Protections against DoS attacks must be implemented.*

DoS protections for the application itself are implemented as concrete bounds - output truncation, shell timeouts with process-tree kill, probe walk caps, request timeouts - see V-222594 for the itemized evidence.

**Evidence:** as V-222594

#### V-222670 / APSC-DV-003345 (CAT III) — **NotAFinding**

*The application must provide notifications or alerts when product update and security related patches are available.*

The application provides update notifications: `picoagent upgrade check` reports outdated plugins and (when configured) the application itself, and [upgrade].check_on_startup surfaces the same at session start - deliberately opt-in so air-gapped installs make no network calls.

**Evidence:** picoagent/cli.py:373-433 (upgrade_command, report_available_upgrades); picoagent/plugins/upgrade.py

---

## 4. Findings table (all Opens)

| Vuln_Num | Rule_Ver | Severity | Finding | Where |
|---|---|---|---|---|
| V-222444 | APSC-DV-000650 | CAT II | Built-in shell passes the full process environment to model-run commands; secrets in it land verbatim in the session log (demonstrated) | `picoagent/core/tools.py:398; core/loop.py:243` |
| V-222469 | APSC-DV-000940 | CAT II | Application shutdown is never recorded in the session log; clean exit and crash are indistinguishable in the record | `picoagent/cli.py:368-369` |
| V-222500 | APSC-DV-001280 | CAT II | Session log (the only activity/audit record) is world-readable: 0644 file / 0755 dir under default umask (verified) | `picoagent/core/session.py:38,101` |
| V-222513 | APSC-DV-001430 | CAT II | No digital-signature verification anywhere in plugin install/upgrade; git clone + user-attested sha256 only; pip deps unpinned | `picoagent/plugins/loader.py:246-262,337-340` |
| V-222587 | APSC-DV-002330 | CAT II | Stored conversation data (repo content, tool output, anything a command printed) is plaintext at default permissions; config hardening only in an opt-in plugin | `picoagent/core/session.py; credential_guard.py:430-441` |
| V-222604 | APSC-DV-002510 | CAT I | Model-composed shell commands execute with no authorization gate in the default configuration; all mitigations are opt-in | `picoagent/core/tools.py:396-412` |
| V-222649 | APSC-DV-003180 | CAT III | No code coverage statistics maintained for any release; no coverage tooling in repo or CI | `.github/workflows/security-scan.yml` |
| V-222655 | APSC-DV-003230 | CAT II | Threat model does not exist — declared "Not yet written" by the repo itself | `docs/security/README.md:9-14` |

---

## 5. Probe-mapping audit (`examples/plugins/stig-runner/asd_probes.py`)

The audit parsed the real V6R4 XCCDF and checked every probe in the table against it.

**Finding P-1 — the stated rule count is wrong.** The module docstring says "Rules mapped
here: 38 of 286." The table actually maps **44** rules (50 probes). Six rules' worth of
probes have been added without the docstring (and anything quoting it downstream) being
updated. The count is load-bearing prose — it tells a reader how much of the benchmark the
tool can speak to — so it should be corrected or, better, computed.

**Verified clean — every `serves` quote.** All 50 probes' `serves` strings were checked
fragment-by-fragment (splitting on `...` elisions, whitespace-normalized) against the
`check-content` of the rule each probe claims, in the V6R4 XCCDF itself. **Zero
mismatches**: every quoted fragment appears verbatim in its own rule's check content, and
no probe quotes a different rule's text. The mapping-scope discipline the docstrings
promise is real.

**Verified clean — key hygiene.** All 44 `PROBES` keys exist in the V6R4 benchmark;
`probes_for` normalizes case; no probe claims a rule that is a pure process requirement it
could not speak to.

**Pattern-quality notes (places to look, not defects in the mapping's honesty — the tool's
own contract is that a hit is a place for a human to look and a miss proves nothing):**

* `APSC-DV-000160`'s second probe, `http://(?!localhost|127\.0\.0\.1)`, misses
  `http://[::1]` and `http://0.0.0.0` (loopback spellings it presumably meant to exempt)
  and fires on every XML namespace URI (`http://checklists.nist.gov/...`), which is much of
  why that rule reports 27 hits on this repo. High noise for the reviewer it hands the hits to.
* `APSC-DV-002290`'s non-CSPRNG pattern misses `random.getrandbits`, `random.uniform`, and
  `random.Random(...)` instances (this repo's one real hit, `fake_es.py:60`, matched only
  because of the `random.Random` call's `choice` usage pattern — a seeded PRNG in test code).
* `APSC-DV-002510`'s pattern misses `os.execv*`/`os.spawn*` and argument-list
  `subprocess.run([...])` built by concatenation; it also cannot see this repo's actual
  command-execution surface (`asyncio.create_subprocess_shell` in `tools.py:309`), which is
  the very thing the V-222604 CAT I Open is about. A probe blind to the assessed repo's own
  headline case is worth extending: add `create_subprocess_shell|create_subprocess_exec`.
* `_dependency_count` labels its TOML approximation misleadingly: for this repo's
  `pyproject.toml` (a project with **zero** dependencies) it reports "~8 declared
  dependencies" because the `_KEY_VALUE` fallback counts every top-level `key =` line. The
  `~` flags it as approximate, but "8 vs 0" is not an approximation error, it is counting
  the wrong thing; `[project] dependencies` should be parsed with `tomllib` (already in the
  stdlib this project restricts itself to).

Run results against picoagent itself (all 50 probes, `max_hits=25`): every
credential-shaped, weak-hash, injection-shaped and TLS-verification hit was reviewed; all
resolve to probe pattern definitions themselves, docstring config examples, or planted test
fixtures — none to a live defect in shipped code. The `ci` probes returned the strongest
positive evidence in the table: `security tooling named: bandit, dependabot, semgrep,
trivy` and `sonar`.

---

## 6. What this assessment is not

**It is not a signed checklist.** A DISA determination is a statement an assessor answers
for. This document was produced by an automated agent reading a repository; the stig-runner
plugin this repo ships refuses to record a determination without a human confirming each
one, and that rule is right. Treat every determination here as a *proposed* answer with its
evidence attached, to be confirmed rule-by-rule by a human assessor before any of it enters
a CKL.

**Process rules cannot be answered from a repository, and were not.** The 30 Not_Reviewed
determinations — support commitments, ISSO duties, CM boards, contingency plans, training,
PPSM registration, FIPS validation of the host crypto, data-owner protection requirements
(including both CAT I at-rest crypto rules, V-222588/9) — require organizational artifacts
or interviews that do not live in git. Where the repo carried partial signal (an active
commit cadence for support, a sha256 mechanism relevant to deploy-hashing) it is noted
inside the entry, but none of it was promoted to a determination it could not support.

**Most NotAFindings rest on reading code and running unit tests, not on testing a running
system.** Specifically: V-222585 (fail to secure state) and V-222486 (fail-stop on audit
failure) were determined from code paths and the repo's own fault-simulation tests, not
from live fault injection; V-222609 (input handling) and V-222656 (error handling) rest on
code reading, the 972-test suite, and the CI SAST configuration — no independent fuzzing
was run; V-222596-599 (transmission protection) rest on code, the repo's redirect tests,
and the documented live-server demonstration in the docs — this assessment did not stand
up a hostile gateway. The determinations verified **by execution during this assessment**
are: session file/dir modes (0644/0755), spill-file mode (0600), the shell environment
passthrough (planted key visible), message timestamp structure, the full probe run, the
`serves`-quote audit, and the test-suite pass.

**Not_Applicable was argued from rule text plus a fact about picoagent, in both
directions.** 191 rules are NA — the honest consequence of assessing a local, single-user,
no-logon, no-web, no-SQL, no-SAML CLI against a benchmark written for hosted DoD
applications. Where a rule's *intent* still had a real analog here (audit trail → session
log; XSS → terminal escape injection; execution whitelisting → the model's shell), the
entry says so and either assesses the analog (the session-log NAF/Open cluster) or
cross-references the finding that carries it (V-222604). If this artifact is ever deployed
as a shared or remotely-reachable service — an RPC frontend plugin is an explicitly
supported extension point — the entire session/authentication/account family stops being
NA and this assessment must be redone on that architecture.

**Scope caveats.** Example plugins under `examples/plugins/` were assessed as shipped code
(the CI scan treats them the same way), but a deployment that loads none of them has a
smaller surface — and one that relies on them must remember every one is opt-in, which is
exactly what three of the eight Opens are about. The fake servers under `picoagent/testing/`
and the test tree ship in the sdist but execute in no production path; probe hits inside
them were treated as fixtures, not findings.
