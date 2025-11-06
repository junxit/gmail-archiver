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
    <a href="https://github.com/yourusername/gmail-archiver/actions/workflows/tests.yml">
      <img alt="Tests" src="https://github.com/yourusername/gmail-archiver/actions/workflows/tests.yml/badge.svg">
    </a>
    <a href="https://codecov.io/gh/yourusername/gmail-archiver">
      <img alt="Codecov" src="https://codecov.io/gh/yourusername/gmail-archiver/branch/main/graph/badge.svg">
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

The tool supports both OAuth 2.0 and IMAP authentication methods and is designed to handle large mailboxes efficiently with incremental backups and resumable operations.

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
  - OAuth 2.0 (recommended)
  - IMAP with App Password
  - Configurable credentials management

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
  - For OAuth: Access to Google Cloud Console
  - For IMAP: 2-Step Verification enabled

- **Dependencies**
  - [Google API Client](https://developers.google.com/gmail/api/quickstart/python)
  - [IMAP Client](https://pypi.org/project/imap-tools/)
  - Other dependencies are listed in `requirements.txt`

## 🚀 Installation

### For Homebrew Python Users

If you're using Python installed via Homebrew (which is managed externally), follow these steps:

1. **Create a virtual environment** (recommended):
   ```bash
   # Create a virtual environment in the project directory
   python3 -m venv .venv
   
   # Activate the virtual environment
   # On macOS/Linux:
   source .venv/bin/activate
   # On Windows:
   # .venv\Scripts\activate
   ```

2. **Install dependencies**:
   ```bash
   # Install the package in development mode
   pip install -e .
   
   # Or install directly with all dependencies
   pip install -r requirements.txt
   ```

### Using pip (Global Installation)

If you prefer a global installation (not recommended):

```bash
# Install directly from PyPI
pip install gmail-archiver
```

### From Source (Alternative Method)

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/gmail-archiver.git
   cd gmail-archiver
   ```

2. **Set up a virtual environment** (recommended)
   ```bash
   # Create and activate virtual environment
   python -m venv venv
   
   # On Windows
   .\venv\Scripts\activate
   
   # On macOS/Linux
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

### Verify Installation

```bash
gmail-archiver --version
# or
python -m gmail_archiver.cli --version
```

## 🔐 Authentication

### Option A: OAuth 2.0 (Recommended)

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

### Option B: IMAP (App Password)

1. **Enable 2-Step Verification**
   - Go to your [Google Account Security](https://myaccount.google.com/security)
   - Under "Signing in to Google," select "2-Step Verification"
   - Follow the prompts to enable it

2. **Generate an App Password**
   - Go to [App Passwords](https://myaccount.google.com/apppasswords)
   - Select "Mail" and "Other (Custom name)"
   - Enter "Gmail Archiver" as the name
   - Click "Generate" and copy the 16-character password

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

### Basic Commands

#### Backup Emails

```bash
# Basic backup (OAuth)
gmail-archiver backup --backup-dir ~/gmail-backup

# Using IMAP
gmail-archiver backup --backup-dir ~/gmail-backup --auth-method imap --email your.email@example.com

# Backup with progress display
gmail-archiver backup --backup-dir ~/gmail-backup --progress
```

#### Restore Emails

```bash
# Basic restore (OAuth)
gmail-archiver restore --backup-dir ~/gmail-backup

# Restore to a different label
gmail-archiver restore --backup-dir ~/gmail-backup --label "Restored Emails"
```

### Advanced Usage

#### Backup Options

```bash
# Backup specific labels only
gmail-archiver backup --backup-dir ~/gmail-backup --labels "INBOX,SENT"

# Backup emails after a specific date
gmail-archiver backup --backup-dir ~/gmail-backup --after "2023-01-01"

# Custom batch size for large mailboxes
gmail-archiver backup --backup-dir ~/gmail-backup --batch-size 50

# Exclude specific labels
gmail-archiver backup --backup-dir ~/gmail-backup --exclude-labels "TRASH,SPAM"
```

#### Restore Options

```bash
# Dry run (show what would be restored)
gmail-archiver restore --backup-dir ~/gmail-backup --dry-run

# Restore only specific labels
gmail-archiver restore --backup-dir ~/gmail-backup --labels "IMPORTANT,PROJECTS"

# Skip existing emails
gmail-archiver restore --backup-dir ~/gmail-backup --skip-existing
```

### Configuration File

Create a `config.ini` file in your backup directory:

```ini
[backup]
directory = /path/to/backup
batch_size = 50
state_file = /path/to/state.json

[oauth]
client_secrets = /path/to/client_secrets.json

[imap]
email = your.email@example.com
app_password = your_app_password
```

Then use it with:

```bash
gmail-archiver --config /path/to/config.ini backup
```

## 🔄 Backup Format

The backup directory structure is organized as follows:

```
backup-dir/
├── emails/                 # Raw .eml files
│   ├── 2023/
│   │   ├── 01/
│   │   │   ├── 01/
│   │   │   │   ├── abc123.eml
│   │   │   │   └── def456.eml
│   │   │   └── ...
│   │   └── ...
│   └── ...
├── metadata/              # JSON metadata files
│   ├── emails.json        # Index of all emails
│   └── state.json         # Backup state and progress
└── logs/                  # Log files
    └── backup_20230101_120000.log
```

### Metadata Format

Each email's metadata is stored in `emails.json` with the following structure:

```json
{
  "emails": {
    "<message_id>": {
      "id": "<message_id>",
      "thread_id": "<thread_id>",
      "subject": "Email Subject",
      "from": "sender@example.com",
      "to": ["recipient@example.com"],
      "date": "2023-01-01T12:00:00Z",
      "labels": ["INBOX", "IMPORTANT"],
      "size": 1024,
      "backup_path": "emails/2023/01/01/abc123.eml"
    }
  },
  "stats": {
    "total_emails": 1,
    "total_size": 1024,
    "last_backup_time": "2023-01-01T12:00:00Z"
  }
}
```

## 🐛 Troubleshooting

### Common Issues

1. **Authentication Errors**
   - For OAuth: Ensure `client_secrets.json` is in the correct location
   - For IMAP: Verify 2-Step Verification is enabled and app password is correct
   - Check that the Gmail API is enabled in Google Cloud Console

2. **Rate Limiting**
   - Google has rate limits for API and IMAP access
   - Use `--batch-size` to reduce the number of requests
   - Add delays between batches with `--delay`

3. **Large Attachments**
   - For large mailboxes, use `--batch-size` to process in smaller chunks
   - Consider using `--max-size` to limit attachment sizes

### Viewing Logs

Logs are stored in the `logs` directory by default. You can also enable verbose logging:

```bash
gmail-archiver --log-level DEBUG backup --backup-dir ~/gmail-backup
```

## 🤝 Contributing

Contributions are welcome! Please read our [Contributing Guide](CONTRIBUTING.md) for details on how to submit pull requests, report bugs, or suggest new features.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [Google Gmail API](https://developers.google.com/gmail/api)
- [IMAP Tools](https://pypi.org/project/imap-tools/)
- [Click](https://click.palletsprojects.com/) for the CLI framework
- `--batch-size`: Number of emails to restore in each batch (default: 10)
- `--state-file`: Custom path to the restore state file

#### Authentication Options

- `--auth-method`: Authentication method to use (`oauth` or `imap`)
- `--client-secrets`: Path to OAuth client secrets file (default: `client_secrets.json`)
- `--token`: Path to OAuth token file (default: `token.json`)
- `--email`: Email address for IMAP authentication
- `--app-password`: App password for IMAP authentication
- `--imap-server`: IMAP server address (default: `imap.gmail.com`)

## Backup File Structure

The backup directory will have the following structure:

```
backup-dir/
├── backup_state.json      # Backup state information
├── emails/               # Email files (.eml)
│   ├── YYYY/
│   │   └── MM/
│   │       └── MESSAGE_ID_HASH_SUBJECT.eml
├── metadata/             # Email metadata (.json)
│   └── MESSAGE_ID.json
└── restore_state.json    # Restore state information (if restored)
```

## Testing

To run the test suite:

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run tests
pytest --cov=gmail_archiver tests/
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Acknowledgments

- [Google Gmail API](https://developers.google.com/gmail/api)
- [python-imap-tools](https://github.com/ikvk/imap_tools)
- [Google API Python Client](https://github.com/googleapis/google-api-python-client)
