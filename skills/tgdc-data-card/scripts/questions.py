"""Stable provider questions. Files cannot establish commercial or legal context."""

QUESTIONS = [
    ('rights', 'What rights and permissions allow you to share this selected data with The General Data Company?', 'Rights are provider-declared; local files cannot establish permission.', 'required'),
    ('archive_scope', 'How does this selected scope relate to your full archive, and which parts are excluded?', 'Selected roots do not prove archive completeness.', 'required'),
    ('provider_context', 'What industry, business function and organization produced this data?', 'Business context needs an explicit provider statement.', 'required'),
    ('history', 'What period does the content cover, and how much historical data is retained?', 'Filesystem modification times are not content dates.', 'high'),
    ('systems', 'Which source systems and export processes produced these files?', 'File formats do not establish the source systems.', 'high'),
    ('case_mapping', 'How should records, messages and documents be linked to real cases or workflows?', 'Rows and files are not cases; joins require an explicit mapping.', 'high'),
    ('workflow_outcomes', 'Which stages, decisions and verified outcomes are recorded for each workflow?', 'Textual status values cannot prove workflow completion.', 'high'),
    ('refresh', 'How frequently is this data updated, and can new exports be provided?', 'Local inventory cannot establish future availability.', 'normal'),
    ('privacy', 'What personal or confidential information is present, and which anonymization rules are required?', 'Profiling is not a privacy audit or anonymization.', 'required'),
]


def questions():
    return [{'id': identifier, 'prompt': prompt, 'reason': reason, 'priority': priority}
            for identifier, prompt, reason, priority in QUESTIONS]
