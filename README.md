# baconexp-backup

Weekly offline mirror of the BACoN experiment's Confluence wiki
(`baconexp.atlassian.net`), written to the DAQ machine's RAID.

## Requirements

- Python 3.8+ with `requests`
- `flock` (util-linux), for the cron lock
- An Atlassian API token: https://id.atlassian.com/manage-profile/security/api-tokens

## Setup

```bash
git clone https://github.com/h0t5tuff/baconexp-backup.git
cd baconexp-backup
mkdir -p state
cp config.env.example config.env
chmod 600 config.env
```

Edit `config.env` and set `CONFLUENCE_SITE`, `CONFLUENCE_EMAIL`, `BACKUP_ROOT`
and `REQUIRE_MOUNT`. Then add the token without putting it in shell history:

```bash
read -rsp 'Paste Atlassian API token: ' T; echo
sed -i "s|^CONFLUENCE_API_TOKEN=.*|CONFLUENCE_API_TOKEN=$T|" config.env; unset T
```

Verify against one space before trusting it. Expect `render_errors=0` and
`fetch_errors=0`:

```bash
./baconexp-backup.py --space HOME --out /tmp/test --no-archive
xdg-open /tmp/test/index.html
```

Optional, so `backups/` reaches the data from inside the project folder:

```bash
ln -s "$(grep ^BACKUP_ROOT config.env | cut -d= -f2)" backups
```

## Usage

```bash
./baconexp-backup.py                 # full backup
./baconexp-backup-status             # last run, contents, next scheduled run
xdg-open backups/latest/index.html   # read the wiki
```

## Configuration

`config.env`, mode 600. The script refuses to run if it is readable by anyone else.

| Key | Meaning |
|---|---|
| `CONFLUENCE_SITE` | `https://baconexp.atlassian.net` |
| `CONFLUENCE_EMAIL` | The account that logs into that site. A mismatch is the usual cause of a 401. |
| `CONFLUENCE_API_TOKEN` | The backup captures exactly what this account can see. |
| `BACKUP_ROOT` | Where backups are written |
| `REQUIRE_MOUNT` | Abort unless this path is a live mountpoint. Leave blank to disable. |
| `KEEP_ARCHIVES` | Tarballs to retain (default 12) |

## Output

```
BACKUP_ROOT/
├── latest -> runs/<newest>       start here
├── runs/<UTC-stamp>/             browsable trees
└── archives/baconexp-*.tar.gz    dated tarballs

runs/<stamp>/HOME - BACoN/
├── index.html                    space home, full tree in a sidebar
├── 01 RunLog Run 6/
│   ├── index.html                the page
│   └── _files/                   its attachments, as real files
├── _assets/                      stylesheet + a copy of both scripts
└── _source/                      raw Confluence storage XML
```

## Scheduling

```bash
(crontab -l 2>/dev/null; echo "0 22 * * 5 /usr/bin/flock -n $PWD/state/lock $PWD/baconexp-backup.py >> $PWD/state/cron.log 2>&1") | crontab -
```

Check `crontab -l` afterwards — this appends, so running it twice gives two entries.

Cron only fires if the machine is on at that moment, and does not catch up a
missed run. `baconexp-backup-status` reports red when the newest backup is over
8 days old. On a machine that sleeps, use a systemd timer with `Persistent=true`.

## Failure modes

Designed to fail loudly rather than produce a bad backup.

| Message | Meaning |
|---|---|
| `FATAL: 401 Unauthorized` | Token revoked/expired, or `CONFLUENCE_EMAIL` is not the account that logs in |
| `FATAL: ... is not mounted` | `REQUIRE_MOUNT` is not live. Prevents filling the root filesystem through an empty mountpoint. |
| `FATAL: ... is mode 644` | `chmod 600 config.env` |
| `FATAL: captured zero pages` | Exits non-zero rather than let a silent auth failure replace a good backup with an empty one |

## License

MIT.
