# Account deletion email sender check

On 20 November 2026, the real Celery task was invoked twice at source commit
58f877408d with a mocked HTTP transport and a synthetic recipient.

With the Resend key empty, the task issued zero HTTP requests. With a synthetic
key configured, the task issued one request with From set to Kria <emir@usekria.com>
and To set to recipient@example.invalid. Both assertions passed.

Live delivery requires a separate check on the deployed worker.
