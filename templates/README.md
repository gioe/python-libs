# libs/templates

Reusable script templates for services in this monorepo. Copy a template into
your service, fill in the placeholder variables, and you're ready to go.

---

## run_cron.sh.template

A battle-tested cron wrapper script that handles:

- Virtual environment activation
- `.env` file loading
- Required environment variable validation
- Structured log output with timestamps
- Proper exit-code propagation
- Optional email failure notification (commented out, ready to enable)

### 5-Step Copy-and-Configure Workflow

**Step 1 — Copy the template into your service**

```bash
cp libs/templates/run_cron.sh.template your-service/scripts/run_cron.sh
chmod +x your-service/scripts/run_cron.sh
```

**Step 2 — Set `SERVICE_NAME`**

Open `your-service/scripts/run_cron.sh` and replace `{{SERVICE_NAME}}` with a
human-readable label shown in log banners:

```bash
SERVICE_NAME="My Service"
```

**Step 3 — Set `REQUIRED_ENV_VARS`**

Replace `{{REQUIRED_ENV_VARS}}` with a space-separated list of environment
variable names that must be non-empty before the script is allowed to run:

```bash
REQUIRED_ENV_VARS="DATABASE_URL API_KEY SECRET_TOKEN"
```

The wrapper will exit with code `3` and a clear error message if any listed
variable is missing at run time.

**Step 4 — Set `SCRIPT_PATH` and `PYTHON_ARGS`**

Replace `{{SCRIPT_PATH}}` with the Python entry-point path (relative to the
service root) and `{{PYTHON_ARGS}}` with the arguments to pass it:

```bash
SCRIPT_PATH="run_my_job.py"
PYTHON_ARGS="--count 100 --verbose"
```

**Step 5 — Add to crontab**

```cron
# Run nightly at 02:00
0 2 * * * /absolute/path/to/your-service/scripts/run_cron.sh >> /var/log/my-service/cron.log 2>&1
```

---

### Canonical Example

`question-service/scripts/run_cron.sh` is the reference implementation that
was used to derive this template. Refer to it for a concrete example of the
four placeholder values filled in for a real service.
