# Mailbox Worker

A Cloudflare Email Worker. Email Routing gives it every message sent to the
zone; it passes mail for `lacre-<id>@in-sidr.xyz` to the gateway's
`POST /inbound`, signed with a shared secret, and rejects everything else.
When the gateway does not answer 2xx the message is rejected too, so the
sender gets a bounce. The contract is in the gateway README, "Mailboxes".

It logs one line per message: the mailbox id (or null), the size and the
gateway status. Never a header, the body, the sender or the address.

## Set up

Once, on the `in-sidr.xyz` zone in the Cloudflare dashboard:

1. Email > Email Routing > enable it. Cloudflare adds the MX and SPF
   records it needs; accept them. Any other MX records for the zone have
   to go.
2. Generate the secret and give it to the gateway, in
   `/etc/lacre-gateway/gateway.env`:

   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   # LACRE_INBOUND_SECRET=<that value>
   systemctl restart lacre-gateway
   ```

3. From this directory, with wrangler logged in to the account:

   ```
   npx wrangler deploy
   npx wrangler secret put LACRE_INBOUND_SECRET    # paste the same value
   ```

   `wrangler.toml` holds the domain, the gateway URL and the size cap;
   change them there if they differ. The secret is never in that file.
4. Email > Email Routing > Routing rules > Catch-all address: action
   "Send to a Worker", destination `lacre-mailboxes`, and enable it.
   Wrangler cannot create this rule; it is what binds the Worker to mail.

The catch-all hands the Worker mail for every address on the zone. The
Worker checks the address shape itself so that only `lacre-` addresses are
ever read and signed; the rest are rejected where they arrive. Addresses
with their own routing rules keep them, since the catch-all only takes what
no other rule matches.

## Test with a real message

```
KEY=<an API key>
curl -s -X POST -H "X-API-Key: $KEY" -F extract=none https://lacre.in-sidr.xyz/mailboxes
```

Send a message that carries a DKIM signature (from Gmail, for instance) to
the `address` in the answer. Then:

```
npx wrangler tail lacre-mailboxes     # {"mailbox":"<id>","size":...,"status":202}
curl -s -H "X-API-Key: $KEY" https://lacre.in-sidr.xyz/mailboxes/<id>/jobs
```

The job has `"via": "inbound"` and the mailbox id. A message to
`lacre-aaaaaaaaaaaa@in-sidr.xyz` (no such mailbox) comes back as a bounce
and the tail shows status 404; one to a disabled mailbox does the same, and
its `dropped` count goes up.
