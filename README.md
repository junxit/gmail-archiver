<div align="center">
  <h1>📧 Gmail Archiver</h1>
  <p>
    <strong>A robust command-line tool to backup and restore Gmail emails with full metadata preservation</strong>
  </p>
  <p>
    <a href="#features">Features</a> •
    <a href="#prerequisites">Prerequisites</a> •
    <a href="#installation">Installation</a> •
    <a href="#authentication">Authentication</a> •
    <a href="#usage">Usage</a> •
    <a href="#backup-format">Backup Format</a> •
    <a href="#troubleshooting">Troubleshooting</a> •
    <a href="#contributing">Contributing</a>
  </p>
  <p>
    <a href="https://pypi.org/project/gmail-archiver/">
      <img alt="PyPI" src="https://img.shields.io/pypi/v/gmail-archiver">
    </a>
    <a href="https://github.com/junxit/gmail-archiver/actions/workflows/tests.yml">
      <img alt="Tests" src="https://github.com/junxit/gmail-archiver/actions/workflows/tests.yml/badge.svg">
    </a>
    <a href="https://codecov.io/gh/junxit/gmail-archiver">
      <img alt="Codecov" src="https://codecov.io/gh/junxit/gmail-archiver/branch/main/graph/badge.svg">
    </a>
    <a href="https://github.com/psf/black">
      <img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg">
    </a>
  </p>
</div>

Gmail Archiver is a powerful command-line tool that allows you to:

- **Backup** your Gmail emails with all metadata (labels, timestamps, etc.)
- **Restore** emails back to Gmail with their original structure
- **Migrate** emails between accounts
- **Archive** important communications for compliance or record-keeping

The tool supports multiple authentication methods including browser-based OAuth, traditional OAuth 2.0, and IMAP with app passwords.

## ✨ Features

- **Complete Email Backup**
  - Download emails as standard `.eml` files
  - Preserve all metadata (labels, timestamps, message structure)
  - Handle large attachments efficiently

- **Smart Restoration**
  - Restore emails with original metadata
  - Preserve folder/label structure
  - Handle duplicate detection

- **Reliable & Efficient**
  - **Incremental Backups** - Only download new or modified emails
  - **Resumable Operations** - Continue interrupted transfers
  - **Batch Processing** - Process emails in configurable batches

- **Flexible Authentication**
  - Browser-based OAuth (easiest - no setup required)
  - OAuth 2.0 with custom credentials
  - IMAP with App Password

- **Advanced Features**
  - Filter emails by date range, labels, or search queries
  - Detailed logging and progress tracking
  - Configurable backup directory structure
  - State management for reliable operation

## 🛠 Prerequisites

- **Python 3.8 or higher**
  - Check your Python version: `python --version`
  - [Download Python](https://www.python.org/downloads/) if needed

- **Google Account**
  - A Gmail account with sufficient storage
  - For OAuth: Access to Google Cloud Console (optional)
  - For IMAP: 2-Step Verification enabled

## 🚀 Installation

### Using uv (Recommended)

[uv](https://github.com/astral-sh/uv) is a fast Python package manager. If you don't have it installed:

```bash
# Install uv (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with Homebrew
brew install uv
```

Then install Gmail Archiver:

```bash
# Create a virtual environment and install
uv venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e .

# Or install dependencies only
uv pip install -r requirements.txt
```

### Using pip

```bash
# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install the package in development mode
pip install -e .

# Or install directly with all dependencies
pip install -r requirements.txt
```

### From PyPI

```bash
# Using uv
uv pip install gmail-archiver

# Using pip
pip install gmail-archiver
```

### From Source

1. **Clone the repository**
   ```bash
   git clone https://github.com/junxit/gmail-archiver.git
   cd gmail-archiver
   ```

2. **Set up virtual environment and install**
   
   Using uv:
   ```bash
   uv venv .venv
   source .venv/bin/activate
   uv pip install -e .
   ```
   
   Using pip:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -e .
   ```

### Verify Installation

```bash
# Using the installed command
gmail-archiver --version

# Or run directly with Python
python -m gmail_archiver.cli --version

# Or with uv (no activation needed)
uv run gmail-archiver --version
```

## 🔐 Authentication

Gmail Archiver supports three authentication methods:

### Option A: Browser-Based OAuth (Easiest)

This is the simplest option - just run the command and a browser window will open for you to sign in:

```bash
gmail-archiver backup --auth-method browser --backup-dir ~/gmail-backup
```

- ✅ No Google Cloud Console setup required
- ✅ Opens browser for secure Google login
- ✅ Tokens are saved for future use

> **Note:** If you have a `client_secrets.json` file in your directory, it will be used automatically. Otherwise, you'll need to set up your own OAuth credentials (see Option B).

### Option B: OAuth 2.0 with Custom Credentials

For more control or organizational use, set up your own OAuth credentials:

1. **Create a Google Cloud Project**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Click "Create Project" and follow the prompts

2. **Enable Gmail API**
   - Navigate to "APIs & Services" > "Library"
   - Search for "Gmail API" and click "Enable"

3. **Configure OAuth Consent Screen**
   - Go to "APIs & Services" > "OAuth consent screen"
   - Select "External" and click "Create"
   - Fill in the required app information
   - Add `https://mail.google.com/` to the authorized domains
   - Add your email as a test user

4. **Create OAuth Credentials**
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "OAuth client ID"
   - Select "Desktop app" as the application type
   - Download the JSON file and save it as `client_secrets.json`

5. **Run with OAuth**
   ```bash
   gmail-archiver backup --auth-method oauth --client-secrets client_secrets.json --backup-dir ~/gmail-backup
   ```

### Option C: IMAP (App Password) — backup only, no OAuth

Back up over IMAP using a Google **App Password**. This path never uses OAuth and
produces the **exact same on-disk format** as the OAuth/browser backups, so the
restore command can read IMAP-made backups unchanged.

1. **Enable 2-Step Verification**
   - Go to your [Google Account Security](https://myaccount.google.com/security)
   - Under "Signing in to Google," select "2-Step Verification"
   - Follow the prompts to enable it

2. **Generate an App Password**
   - Go to [App Passwords](https://myaccount.google.com/apppasswords)
   - Select "Mail" and "Other (Custom name)"
   - Enter "Gmail Archiver" as the name
   - Click "Generate" and copy the 16-character password

3. **Run the backup**
   ```bash
   gmail-archiver backup \
     --auth-method imap \
     --email your.email@gmail.com \
     --app-password "xxxx xxxx xxxx xxxx" \
     --backup-dir ~/gmail-backup

   # Or with uv (no virtual environment activation needed):
   uv run gmail-archiver backup --auth-method imap \
     --email your.email@gmail.com --app-password "xxxx xxxx xxxx xxxx" \
     --backup-dir ~/gmail-backup
   ```

   - **All Mail by default:** the IMAP backup reads Gmail's `[Gmail]/All Mail`
     folder, so every archived message is captured — not just the inbox. Override
     with `--folder` (the exact name can vary by account language), for example
     `--folder "INBOX"`.
   - **Incremental:** re-running skips messages already backed up (keyed by
     Gmail's permanent `X-GM-MSGID`), so only new mail is downloaded.
   - **Never marks mail as read:** the mailbox is opened read-only and messages
     are fetched with `BODY.PEEK[]`.
   - **Your app password is never logged** and is never written into the backup.

> **Restore is OAuth/browser-only.** IMAP is for backup only. Restore an
> IMAP-made backup with OAuth or the browser flow, e.g.
> `gmail-archiver restore --auth-method browser --backup-dir ~/gmail-backup`.

#### IMAP label preservation caveats

IMAP exposes Gmail labels via the `X-GM-LABELS` extension. The backup normalizes
them to match the API path's label vocabulary as closely as IMAP allows:

| Aspect | Behavior |
|---|---|
| **System labels** | Mapped to the same ids the API uses (`\Inbox`→`INBOX`, `\Sent`→`SENT`, `\Important`→`IMPORTANT`, `\Starred`→`STARRED`, `\Draft`→`DRAFT`, `\Trash`→`TRASH`, `\Junk`/`\Spam`→`SPAM`). |
| **Read state** | `UNREAD` is synthesized from the IMAP `\Seen` flag (IMAP read state is a flag, not a label). |
| **User labels** | Stored by **name**, because IMAP exposes label names rather than the API's internal `Label_NNN` ids. On restore these names are not valid Gmail label ids, so re-applying user labels is best-effort — message content and system labels restore normally. |
| **Categories** | Gmail categories (`CATEGORY_*`) are not exposed via `X-GM-LABELS`, so they are absent from IMAP-made backups. |

### Environment Variables (Optional)

You can set the following environment variables for easier authentication:

```bash
# For OAuth
export GMAIL_ARCHIVER_CLIENT_SECRETS=path/to/client_secrets.json

# For IMAP
export GMAIL_ARCHIVER_EMAIL=your.email@example.com
export GMAIL_ARCHIVER_APP_PASSWORD=your_app_password
```

## 💻 Usage

> **Flexible argument order:** global options (`--auth-method`, `--backup-dir`,
> `--email`, `--app-password`, `--folder`, `--log-level`, …) may be given either
> before or after the `backup`/`restore` subcommand — e.g.
> `gmail-archiver backup --auth-method imap …` and
> `gmail-archiver --auth-method imap … backup` are equivalent. The examples below
> place them after the subcommand.

### Basic Commands

#### Backup Emails

```bash
# Browser-based authentication (easiest)
gmail-archiver backup --auth-method browser --backup-dir ~/gmail-backup

# Using OAuth with custom credentials
gmail-archiver backup --auth-method oauth --client-secrets ~/credentials.json --backup-dir ~/gmail-backup

# Using IMAP (App Password) — backs up [Gmail]/All Mail by default, incremental on re-run
gmail-archiver backup --auth-method imap --email your.email@gmail.com --app-password "xxxx xxxx xxxx xxxx" --backup-dir ~/gmail-backup

# IMAP: back up a specific folder instead of All Mail
gmail-archiver backup --auth-method imap --email your.email@gmail.com --app-password "xxxx xxxx xxxx xxxx" --folder "INBOX" --backup-dir ~/gmail-backup

# With uv (no virtual environment activation needed)
uv run gmail-archiver backup --auth-method browser --backup-dir ~/gmail-backup
```

#### Restore Emails

```bash
# Restore from backup
gmail-archiver restore --backup-dir ~/gmail-backup

# With uv
uv run gmail-archiver restore --backup-dir ~/gmail-backup
```

### Advanced Usage

#### Backup Options

```bash
# Limit number of emails to backup
gmail-archiver backup --backup-dir ~/gmail-backup --max-results 100

# Custom batch size for large mailboxes
gmail-archiver backup --backup-dir ~/gmail-backup --batch-size 50

# Enable debug logging
gmail-archiver --log-level DEBUG backup --backup-dir ~/gmail-backup
```

#### Restore Options

```bash
# Limit number of emails to restore
gmail-archiver restore --backup-dir ~/gmail-backup --max-results 50

# Custom batch size
gmail-archiver restore --backup-dir ~/gmail-backup --batch-size 5
```

## � Backup Format

The backup directory structure is organized as follows:

```
backup-dir/
├── emails/                 # Raw .eml files organized by date
│   └── YYYY/
│       └── MM/
│           └── MESSAGE_ID_HASH_SUBJECT.eml
├── metadata/               # JSON metadata files
│   └── MESSAGE_ID.json
├── backup_state.json       # Backup progress and state
└── restore_state.json      # Restore progress (if restored)
```

### Metadata Format

Each email's metadata is stored as a JSON file:

```json
{
  "message_id": "<message_id>",
  "thread_id": "<thread_id>",
  "subject": "Email Subject",
  "from": "sender@example.com",
  "to": "recipient@example.com",
  "date": "Mon, 1 Jan 2024 12:00:00 +0000",
  "labels": ["INBOX", "IMPORTANT"],
  "size": 1024,
  "backup_path": "emails/2024/01/abc123.eml",
  "backup_time": "2024-01-01T12:00:00Z"
}
```

> Both the OAuth/browser and IMAP backends write this **identical** schema and
> directory layout, so a backup made by either is restorable by the same restore
> command. IMAP-made backups differ only in label vocabulary (user labels and
> categories) — see [IMAP label preservation caveats](#imap-label-preservation-caveats).

## 🐛 Troubleshooting

### Common Issues

1. **Authentication Errors**
   - For browser auth: Ensure you complete the Google sign-in in the browser
   - For OAuth: Ensure `client_secrets.json` is in the correct location
   - For IMAP: Verify 2-Step Verification is enabled and app password is correct
   - Check that the Gmail API is enabled in Google Cloud Console

2. **Rate Limiting**
   - Google has rate limits for API and IMAP access
   - Use `--batch-size` to reduce the number of requests
   - Add delays between batches if needed

3. **Large Attachments**
   - For large mailboxes, use `--batch-size` to process in smaller chunks
   - Consider using `--max-results` to limit the scope

### Viewing Logs

Enable verbose logging for debugging:

```bash
gmail-archiver --log-level DEBUG backup --backup-dir ~/gmail-backup
```

## � Testing

Run the test suite:

```bash
# Using uv
uv run pytest --cov=gmail_archiver tests/

# Using pip
pip install pytest pytest-cov
pytest --cov=gmail_archiver tests/

# Run specific test files
pytest tests/test_backup.py -v
pytest tests/test_restore.py -v
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [Google Gmail API](https://developers.google.com/gmail/api)
- [python-imap-tools](https://github.com/ikvk/imap_tools)
- [Google API Python Client](https://github.com/googleapis/google-api-python-client)
- [uv](https://github.com/astral-sh/uv) - Fast Python package manager
