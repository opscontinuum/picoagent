"""Data only: which Application Security and Development STIG rules a repository can speak to.

Every ``serves`` string is quoted from the ``Check_Content`` of that rule in ASD V6R4
(``Release: 4 Benchmark Date: 01 Oct 2025``), read from a checklist exported by STIG Viewer
3.7.0. The quote is there so the probe's *scope* is visible: the probe covers the sentence it
quotes and nothing else in the check procedure, and the tool prints the quote alongside the
hits for exactly that reason.

What this table is not
----------------------
It is not a compliance oracle and it is not a SAST engine. Every probe is a file listing or a
regex. A hit is a place for a human to look; a miss means the probe found nothing, not that
the requirement is met. ASD V6R4 has 286 rules and the majority of them are process
requirements - threat models, code review records, training, design documents, ISSO duties -
which no amount of grep can answer. Those rules are absent from this table on purpose, and
``stig_evidence`` says so in as many words when it is asked about one.

Rules mapped here: 38 of 286. Everything else returns "no automated probe".
"""
from __future__ import annotations

from evidence import Probe

#: Common file globs, named so the table below reads as intent rather than punctuation.
CODE = ("*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.java", "*.cs", "*.go", "*.rb", "*.php",
        "*.kt", "*.scala", "*.c", "*.cc", "*.cpp", "*.h", "*.rs", "*.swift", "*.m")
CONFIG = ("*.yml", "*.yaml", "*.json", "*.toml", "*.ini", "*.cfg", "*.conf", "*.properties",
          "*.xml", "*.env", ".env*", "web.config", "appsettings*.json")
CODE_AND_CONFIG = CODE + CONFIG

#: ``Rule_Ver`` -> the probes that speak to it. Order within a rule is the order it is reported.
PROBES: dict[str, list[Probe]] = {

    # ---------------------------------------------------------------- sessions and logon
    "APSC-DV-000010": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"max[_-]?(concurrent[_-]?)?sessions|concurrent[_-]?session|session[_-]?limit"
                      r"|maximumSessions|max-sessions",
              serves="examine configuration files in order to review user session configuration "
                     "settings"),
    ],
    "APSC-DV-000060": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"session\.(invalidate|destroy|clear)|clear[_-]?session|delete_cookie"
                      r"|res\.clearCookie|SESSION_COOKIE_",
              serves="identify how the application makes use of temporary client storage and "
                     "cookies"),
    ],
    "APSC-DV-000070": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"(idle|session|inactivity)[_-]?(time_?out|lifetime|expiry|expire|age)"
                      r"|PERMANENT_SESSION_LIFETIME|session-timeout",
              serves="demonstrate the configuration setting where the idle time out value is "
                     "defined"),
    ],
    "APSC-DV-000080": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"admin[_-]?(idle|session)[_-]?(time_?out|lifetime|expiry)"
                      r"|(idle|session)[_-]?time_?out",
              serves="the application configuration setting where the idle time out value is "
                     "defined for admin users"),
    ],
    "APSC-DV-000090": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"\b(log_?out|sign_?out|logoff)\b",
              serves="Identify the command or link that provides the logoff function"),
    ],

    # ---------------------------------------------------------------- transport and crypto
    "APSC-DV-000160": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"verify\s*=\s*False|InsecureSkipVerify|TrustAllCerts|NODE_TLS_REJECT_UNAUTHORIZED"
                      r"|rejectUnauthorized\s*:\s*false|CURLOPT_SSL_VERIFYPEER\s*,\s*(0|false)"
                      r"|ServerCertificateValidationCallback",
              serves="Review application configuration settings to ensure encryption is specified "
                     "and via TLS"),
        Probe(kind="grep", globs=CODE_AND_CONFIG, pattern=r"http://(?!localhost|127\.0\.0\.1)",
              serves="determine if the session is protected via TLS"),
    ],
    "APSC-DV-002030": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"\b(md5|sha1|sha-1)\b|MessageDigest\.getInstance\(\s*[\"'](MD5|SHA-?1)",
              serves="what hashing algorithms are used when generating a hash value ... DoD PKI "
                     "policy prohibits the use of SHA1 as of December 2016"),
    ],
    "APSC-DV-002040": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"\b(DES|3DES|RC4|Blowfish|ECB)\b|MODE_ECB|AES/ECB|Cipher\.getInstance",
              serves="identify the cryptographic modules used by the application ... determine if "
                     "the cryptographic modules used by the application have been FIPS-validated"),
    ],
    "APSC-DV-002290": [
        Probe(kind="grep", globs=CODE,
              pattern=r"\brandom\.(random|randint|choice|randrange)\s*\(|Math\.random\s*\(\)"
                      r"|new\s+Random\s*\(",
              serves="determine if the application server uses a FIPS 140-2/140-3 approved random "
                     "number generator to create unique session identifiers"),
    ],
    "APSC-DV-002300": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"verify\s*=\s*False|InsecureSkipVerify|check_hostname\s*=\s*False"
                      r"|CERT_NONE|TrustManager\s*\[\s*\]",
              serves="If the application utilizes PKI certificates other than DoD-approved PKI and "
                     "ECA certificates, this is a finding"),
        Probe(kind="exists", globs=("*.pem", "*.crt", "*.cer", "*.p12", "*.pfx", "*.jks"),
              serves="identify certificate location"),
    ],
    "APSC-DV-001810": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"verify\s*=\s*False|InsecureSkipVerify|CERT_NONE|check_hostname\s*=\s*False"
                      r"|X509TrustManager|checkServerTrusted",
              serves="identify the method employed by the application for validating certificates "
                     "... determine if a certification path that includes status information is "
                     "constructed"),
    ],
    "APSC-DV-002440": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"\bssl_version\b|TLSv1(\.[01])?\b|SSLv[23]|PROTOCOL_TLSv1(_1)?\b|MinVersion"
                      r"|ssl_protocols",
              serves="If the application does not utilize TLS, IPsec or other approved encryption "
                     "mechanism to protect the confidentiality and integrity of transmitted "
                     "information, this is a finding"),
    ],

    # ---------------------------------------------------------------- passwords and secrets
    "APSC-DV-001680": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"(min|minimum)[_-]?(password|pwd)?[_-]?length|password[_-]?min[_-]?length"
                      r"|MinimumLength|PASSWORD_MIN_LENGTH",
              serves="attempt to create a password shorter than 15 characters in length"),
    ],
    "APSC-DV-001740": [
        Probe(kind="grep", globs=CODE,
              pattern=r"bcrypt|scrypt|argon2|pbkdf2|password_hash\s*\(|BCryptPasswordEncoder"
                      r"|hashlib\.(md5|sha1)\s*\(",
              serves="Determine if password strings are readable/discernable. Determine if the "
                     "application uses the MD5 hashing algorithm to create password hashes"),
    ],
    "APSC-DV-001750": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"http://[^\s\"']*(login|auth|signin|token)",
              serves="verify the web browser has gone secure prior to entering any password or "
                     "authentication information"),
    ],
    "APSC-DV-001850": [
        Probe(kind="grep", globs=CODE + ("*.html", "*.htm", "*.jsx", "*.tsx", "*.vue", "*.jsp"),
              pattern=r"type\s*=\s*[\"']text[\"'][^>]*(password|passwd|pin)"
                      r"|(password|passwd)[^>]*type\s*=\s*[\"']text[\"']",
              serves="verify any display feedback provided when the admin enters her/his password "
                     "is obfuscated and not clear text"),
    ],
    "APSC-DV-003110": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*"
                      r"[\"'][^\"'\s]{6,}[\"']",
              serves="Identify any instances of passwords, certificates, or sensitive data "
                     "included in code"),
        Probe(kind="exists", globs=(".env", ".env.*", "*.pem", "id_rsa", "id_dsa", "id_ecdsa",
                                    "*.p12", "*.pfx", "credentials", "*.keystore"),
              serves="this includes configuration files such as global.asa, if present, scripts, "
                     "HTML files, and any ASCII files"),
    ],
    "APSC-DV-003280": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"(pass(word|wd)?)\s*[:=]\s*[\"']?(admin|password|changeme|secret|letmein"
                      r"|root|test|123456|default)[\"']?\s*$",
              serves="If default passwords are found, attempt to authenticate with the published "
                     "default passwords"),
    ],

    # ---------------------------------------------------------------- session identifiers
    "APSC-DV-002210": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"http_?only|HttpOnly|SESSION_COOKIE_HTTPONLY",
              serves="configuring the web server with Mod_Security or ESAPI WAF with the HTTPOnly "
                     "flag directives enabled"),
    ],
    "APSC-DV-002220": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"SESSION_COOKIE_SECURE|cookie[_-]?secure|secure\s*[:=]\s*(true|false)"
                      r"|\bsameSite\b",
              serves="Verify that the scan configuration includes checks for the secure flag on "
                     "session cookies"),
    ],
    "APSC-DV-002230": [
        Probe(kind="grep", globs=CODE + ("*.log",),
              pattern=r"(log|print|console\.log|logger)[^\n]*(session[_-]?id|sessionid|JSESSIONID)",
              serves="Identify the application communication paths ... that transmit session "
                     "identifiers over the network"),
    ],
    "APSC-DV-002240": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"session\.(invalidate|destroy|clear)\s*\(|req\.session\.destroy"
                      r"|SecurityContextHolder\.clearContext",
              serves="Review framework configuration setting to determine how the session "
                     "identifiers are destroyed"),
    ],
    "APSC-DV-002270": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"jsessionid=|sessionid=|;jsessionid|url[_-]?rewriting|disable-url-rewriting"
                      r"|tracking-mode",
              serves="if the framework or the application is configured to transmit cookies within "
                     "the URL or via URL rewriting ... this is a finding"),
    ],

    # ---------------------------------------------------------------- logging and audit
    "APSC-DV-000650": [
        Probe(kind="grep", globs=CODE,
              pattern=r"(log|logger|logging)[^\n]*\b(password|passwd|secret|token|ssn|credit[_-]?card)\b",
              serves="create search strings that could successfully identify the existence of "
                     "passwords, session IDs, or other sensitive information such as SSN"),
    ],
    "APSC-DV-000670": [
        Probe(kind="grep", globs=CONFIG + ("*.py", "*.java", "*.js", "*.ts", "*.cs", "*.go"),
              pattern=r"asctime|%\(asctime\)|timestamp|%d\{|ISO8601|logging\.Formatter",
              serves="If the time the event occurred is not included as part of the event, this is "
                     "a finding"),
    ],
    "APSC-DV-000700": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"(log|logger)[^\n]*\b(user_?id|username|principal|subject|actor)\b",
              serves="Observe if the log includes an entry to indicate the user ID of the user "
                     "that conducted the activity"),
    ],
    "APSC-DV-000830": [
        Probe(kind="grep", globs=CODE,
              pattern=r"(log|logger|audit)[^\n]*\b(login|logon|authenticat|sign[_-]?in|failed)\b",
              serves="If successful and unsuccessful logon events are not recorded in the logs, "
                     "this is a finding"),
    ],
    "APSC-DV-001010": [
        Probe(kind="grep", globs=CODE,
              pattern=r"(log|logger)[^\n]*\b(SUCCESS|FAILURE|ERROR|PASS|outcome|result)\b",
              serves="a log record that displays the application event/operation that occurred "
                     "followed by the result of the operation such as \"ERROR\", \"FAILURE\", "
                     "\"SUCCESS\" or \"PASS\""),
    ],
    "APSC-DV-001080": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"syslog|SyslogHandler|fluentd|fluent-bit|logstash|filebeat|splunk"
                      r"|cloudwatch|opentelemetry|otlp|GELF|graylog",
              serves="determine if the system is configured to utilize a centralized log "
                     "management system for the hosting and management of application audit logs"),
    ],

    # ---------------------------------------------------------------- input handling
    "APSC-DV-002490": [
        Probe(kind="grep", globs=CODE + ("*.html", "*.jsx", "*.tsx", "*.vue", "*.jsp", "*.erb"),
              pattern=r"innerHTML\s*=|dangerouslySetInnerHTML|v-html|\|\s*safe\b|mark_safe"
                      r"|document\.write\s*\(|\bautoescape\s*(=|:)\s*(false|off)",
              serves="perform manual testing in various data entry fields to determine if XSS exist"),
        Probe(kind="ci",
              serves="Review the application documentation and the vulnerability assessment scan "
                     "results from automated vulnerability assessment tools"),
    ],
    "APSC-DV-002500": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"csrf|CsrfToken|XSRF|SameSite|csrf_exempt|WithCredentials",
              serves="Review the scan results for CSRF vulnerabilities ... web application "
                     "firewalls that validate cookie and the referrer field in the HTTP headers"),
    ],
    "APSC-DV-002510": [
        Probe(kind="grep", globs=CODE,
              pattern=r"os\.system\s*\(|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"
                      r"|Runtime\.getRuntime\(\)\.exec|child_process\.exec\s*\(|\bpopen\s*\(|eval\s*\(",
              serves="automated code review and vulnerability scans conducted to test for command "
                     "injection"),
    ],
    "APSC-DV-002530": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"\b(pydantic|marshmallow|cerberus|joi|zod|yup|jsonschema|@Valid|Validated"
                      r"|FluentValidation|express-validator|validate_input)\b",
              serves="Verify scan configuration settings include input validation and fuzzing tests"),
    ],
    "APSC-DV-002540": [
        Probe(kind="grep", globs=CODE,
              pattern=r"(execute|query|rawQuery|createStatement|prepare)\s*\([^)]*"
                      r"(\+\s*[A-Za-z_]|%\s*[\(A-Za-z_]|\.format\(|f[\"']|\$\{)"
                      r"|SELECT\s+.*\+|WHERE\s+.*[\"']\s*\+",
              serves="Verify the scan configuration is configured to test for SQL injection flaws"),
    ],
    "APSC-DV-002550": [
        Probe(kind="grep", globs=CODE,
              pattern=r"XMLReader|DocumentBuilderFactory|SAXParser|etree\.(parse|fromstring)"
                      r"|xml\.dom\.minidom|resolve_entities|XXE|DTD",
              serves="verify the scan was configured to test for XML-related vulnerabilities and "
                     "security issues ... XML Injection XML related Denial of Service XPATH "
                     "injection"),
    ],
    "APSC-DV-002485": [
        Probe(kind="grep", globs=("*.html", "*.htm", "*.jsx", "*.tsx", "*.vue", "*.jsp", "*.erb"),
              pattern=r"type\s*=\s*[\"']hidden[\"']",
              serves="Examine identified hidden fields and determine what type of data is stored "
                     "in the hidden fields"),
    ],

    # ---------------------------------------------------------------- errors and disclosure
    "APSC-DV-002480": [
        Probe(kind="grep", globs=CODE_AND_CONFIG,
              pattern=r"\bDEBUG\s*[:=]\s*(True|true|1)\b|app\.debug\s*=\s*True|display_errors\s*=\s*On"
                      r"|customErrors\s+mode\s*=\s*[\"']Off",
              serves="Review web server configuration and determine if custom error pages are "
                     "configured to display on error events"),
    ],
    "APSC-DV-002570": [
        Probe(kind="grep", globs=CODE,
              pattern=r"printStackTrace\s*\(|traceback\.(print_exc|format_exc)|\.getStackTrace"
                      r"|e\.stack\b|str\(e\)|exc\.args",
              serves="If variable names, SQL strings, system path information, or source or "
                     "program code are displayed in error messages sent to non-privileged users, "
                     "this is a finding"),
    ],

    # ---------------------------------------------------------------- build, deps, process
    "APSC-DV-001460": [
        Probe(kind="ci",
              serves="obtain and review their application vulnerability scanning process. Request "
                     "the latest scan results including scan configuration settings"),
    ],
    "APSC-DV-002630": [
        Probe(kind="manifest",
              serves="If application updates are not checked on at least on a weekly basis and "
                     "applied immediately or in accordance with POA&Ms, IAVMs, CTOs, DTMs or other "
                     "authoritative patching guidelines or sources, this is a finding"),
        Probe(kind="exists", globs=(".github/dependabot.yml", ".github/dependabot.yaml",
                                    "renovate.json", ".renovaterc", ".renovaterc.json"),
              serves="inquire about patching process"),
    ],
    "APSC-DV-003170": [
        Probe(kind="ci",
              serves="If code reviews are conducted with software tools, have the application "
                     "representative provide the latest code review report for the application"),
        Probe(kind="exists", globs=("CODEOWNERS", ".github/CODEOWNERS", "CONTRIBUTING.md",
                                    ".github/pull_request_template.md"),
              serves="describe the code review process or provide documentation outlining the "
                     "organizations code review process"),
    ],
    "APSC-DV-003230": [
        Probe(kind="exists", globs=("THREAT_MODEL*", "threat-model*", "threat_model*",
                                    "docs/threat*", "*.tm7"),
              serves="Review the threat model document and identify the following sections are "
                     "present: - Identified threats - Potential vulnerabilities - Counter measures "
                     "taken - Potential mitigations"),
    ],
    "APSC-DV-003235": [
        Probe(kind="ci",
              serves="Identify the most recent security scans and code analysis testing conducted. "
                     "Verify testing configuration includes tests for error handling issues"),
    ],
    "APSC-DV-002960": [
        Probe(kind="exists", globs=("*.conf", "*.ini", "*.properties", "config/*", "conf/*",
                                    "settings.py", "appsettings*.json"),
              serves="Identify the directory where the application code, configuration settings "
                     "and other application control data are located. Identify where user data is "
                     "stored"),
    ],
}


def probes_for(rule_ver: str) -> list[Probe]:
    """The probes for one ``Rule_Ver``. Empty means "no automated probe", which is an answer."""
    return PROBES.get(rule_ver.strip().upper(), [])
