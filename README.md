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
chmod +x baconexp-backup.py baconexp-backup-status
cp config.env.example config.env
chmod 600 config.env
```

Open `config.env` and put the token

Verify against one space before trusting it. Expect `render_errors=0` and
`fetch_errors=0`:

```bash
./baconexp-backup.py --space HOME --out /tmp/test --no-archive
xdg-open /tmp/test/index.html
```

so `backups/` reaches the data from inside the project folder:

```bash
ln -s "$(grep ^BACKUP_ROOT config.env | cut -d= -f2)" backups
```

## Usage

```bash
./baconexp-backup.py                 # full backup
./baconexp-backup-status             # last run, contents, next scheduled run
xdg-open backups/latest/index.html   # read the wiki
```

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

## Troubleshooting

| Symptom                                   | Cause and fix                                                                                                                   |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `./baconexp-backup.py: Permission denied` | The executable bit is missing. `chmod +x baconexp-backup.py baconexp-backup-status`. Or run it as `python3 baconexp-backup.py`. |
| `FATAL: 401 Unauthorized`                 | Token revoked/expired, or `CONFLUENCE_EMAIL` is not the account that logs in.                                                   |
| `FATAL: ... is not mounted`               | `REQUIRE_MOUNT` is wrong or the drive is absent. It must name the mountpoint, not the backup folder.                            |
| `FATAL: ... is mode 644`                  | `chmod 600 config.env`                                                                                                          |
| `FATAL: captured zero pages`              | Deliberate — a silent auth failure must not replace a good backup with an empty one.                                            |

## License

MIT.
