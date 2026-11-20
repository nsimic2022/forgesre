# Sign-in: LDAP / Active Directory and single sign-on (OIDC)

Local accounts (Settings › Users and roles) always work, the installer's Admin included. On top of
them, people can sign in with the company directory or the company's single sign-on. The
directory decides the role on every sign-in, from group membership; ForgeSRE keeps no copy of
their password.

## LDAP / Active Directory

Settings › Sign-in (or `config/forgesre.yml` under `auth.ldap`):

| Setting | Example |
|---|---|
| LDAP server | `ldaps://dc1.dc.local:636` (`ldap://` uses STARTTLS) |
| Service account DN | `CN=svc-forgesre,OU=Service,DC=dc,DC=local` (empty = anonymous search) |
| Service account password | `./forgesre settings set-secret LDAP_BIND_PASSWORD` |
| Search base | `OU=Staff,DC=dc,DC=local` |
| User filter | default `(|(mail={login})(userPrincipalName={login})(sAMAccountName={login}))` |
| Group → Admin / Engineer / Analyst / Viewer | group DNs, e.g. `CN=NOC-Engineers,OU=Groups,DC=dc,DC=local` |
| Role without a matching group | empty (cannot sign in) or `viewer` |

People type their mail, UPN or account name and their directory password on the normal sign-in
page. ForgeSRE finds them with the service account, binds as them, reads `memberOf` and gives the
highest mapped role. **Test connection** binds with the service account and reads the search base.
Each call has a timeout (5 s by default); an unreachable directory is a failed sign-in, never a hang.

## OIDC single sign-on

Works with Entra ID (Azure AD), Keycloak, Okta, Google Workspace, Authentik and other OpenID Connect
providers. Settings › Sign-in (`auth.oidc`):

1. Set **Public address** in Settings › General (e.g. `https://forgesre.dc.local`).
2. Register ForgeSRE at the provider as a web application with the redirect URI
   `https://<public address>/auth/oidc/callback`, and let it put the groups in the ID token.
3. Fill in the issuer (e.g. `https://login.microsoftonline.com/<tenant>/v2.0`), the client id, the
   client secret (`./forgesre settings set-secret OIDC_CLIENT_SECRET`), the groups claim and which
   groups give which role. **Test connection** reads the provider's discovery document and shows the
   redirect URI to register.

The sign-in page then shows the button (its text is a setting). The flow is the authorization code
flow with PKCE; state, nonce and verifier are kept in a signed cookie for 10 minutes, so any Core
process can finish it. The ID token's signature, issuer, audience, expiry and nonce are checked.

## Rules

- A local account and a directory account never mix: if a local user has the same email, only its
  local password works.
- Directory and SSO users get a password hash that can never match, so turning LDAP or OIDC off
  locks them out; local accounts are unaffected.
- Roles are refreshed at every sign-in and every change is in the audit log (`user.create`,
  `user.update` by `ldap` or `oidc`). Disable a person in Users and roles to block them at once.
