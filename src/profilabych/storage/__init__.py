"""Storage boundary: SQLite repositories for history and notes.

Every query must be scoped to a trusted user_id; histories also use chat_id.
Importing this package does not create files or open a database.
"""
