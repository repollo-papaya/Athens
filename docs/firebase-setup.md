# Firebase setup (connection test)

This connects the GitHub Pages site to a Cloud Firestore database and proves it
can read and write. The test leaves no data behind.

Everything here fits in Firebase's free **Spark** plan. No billing account is needed.

## 1. Create the project

1. Go to <https://console.firebase.google.com> and sign in with the Google account
   that should own the project.
2. **Create a project.** Any name works, for example `arc-rocket-optimizer`.
   Google Analytics is not needed; you can turn it off.

## 2. Create the Firestore database

1. In the left menu: **Build > Firestore Database > Create database**.
2. Location: pick one near the team (for example `nam5 (United States)`).
   This cannot be changed later.
3. Start in **production mode**. The rules in step 4 open only what the test needs.

## 3. Turn on anonymous sign-in

The test signs in anonymously so the rules can require a signed-in user. Google
sign-in replaces this later.

1. **Build > Authentication > Get started**.
2. **Sign-in method > Anonymous > Enable > Save**.

## 4. Publish the security rules

1. **Firestore Database > Rules**.
2. Replace everything with the contents of [`firestore.rules`](../firestore.rules).
3. Click **Publish**.

These rules allow signed-in users to create, read and delete only their own
documents in `connection_tests`. Every other collection is closed.

## 5. Register the web app and copy its config

1. **Project settings** (gear icon) **> General > Your apps > Web (`</>`)**.
2. Nickname: `athens-web`. Leave **Firebase Hosting** unchecked; the site is on
   GitHub Pages.
3. Copy the values from the `firebaseConfig` object into
   [`firebase-config.js`](../firebase-config.js) and push to `main`.

These values are public by design. They identify the project; the rules are what
protect the data.

## 6. Run the test

Open <https://repollo-papaya.github.io/Athens/firebase-test.html> and click
**Run connection test**. All five steps should pass, ending with
"The database is empty again."

## Optional: auto-delete leftovers

Every test document has an `expireAt` field one hour ahead. If a test is
interrupted before its delete step, a TTL policy removes the document later:

**Firestore Database > Time-to-live > Create policy**, collection group
`connection_tests`, timestamp field `expireAt`.

## Next

When the connection test passes, the next steps are Google sign-in with an
allowlist and the real collections from the design document
(`jobs`, `rockets`, `flights`, `results`, `calibration`).
