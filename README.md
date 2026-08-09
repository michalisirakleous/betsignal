# Value Bet Telegram Agent

Στέλνει μια φορά την ημέρα (μέσω GitHub Actions) το καλύτερο "value bet"
της ημέρας για ποδόσφαιρο (top 5 λίγκες + Champions League), βασισμένο σε
Poisson στατιστικό μοντέλο vs. τις αποδόσεις της αγοράς.

## 1. Νέο Telegram bot

1. Στο Telegram, μίλα στο **@BotFather** → `/newbot` → δώσε όνομα.
2. Κράτα το **token** που σου δίνει (π.χ. `123456:ABC-DEF...`).
3. Στείλε ένα οποιοδήποτε μήνυμα στο καινούριο bot σου (ή πρόσθεσέ το σε
   ένα κανάλι/group).
4. Βρες το **chat_id** σου:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (άνοιξε το link στον browser αφού έχεις στείλει μήνυμα στο bot — θα δεις
   `"chat":{"id": ...}` στο JSON).

## 2. Δωρεάν API keys

- **football-data.org**: εγγραφή στο https://www.football-data.org/client/register
  (free tier: 10 requests/λεπτό, ΟΛΕΣ τις διοργανώσεις που καλύπτει δωρεάν —
  13 συνολικά: PL, La Liga, Serie A, Bundesliga, Ligue 1, Champions League,
  Championship, Primeira Liga, Eredivisie, Brasileirão, World Cup, Euro,
  Copa Libertadores. Αυτό είναι το ανώτατο όριο κάλυψης χωρίς πληρωμή — δεν
  υπάρχει δωρεάν τρόπος να προστεθούν άλλες λίγκες, π.χ. Κυπριακό, MLS,
  J-League κλπ.).
- **the-odds-api.com**: εγγραφή στο https://the-odds-api.com/
  (free tier: 500 requests/μήνα — το agent κάνει ~10 requests/ημέρα, άρα
  αρκεί άνετα ακόμα κι αν τρέχει κάθε μέρα).
- **api-football.com** (προαιρετικό, για Ελλάδα/Αυστρία/Ελβετία/Πολωνία/
  Τουρκία/Σκωτία/Βέλγιο): εγγραφή στο https://www.api-football.com/
  (free tier: **100 requests/μέρα** — γι' αυτό χρησιμοποιείται ΜΟΝΟ για
  αυτές τις 7 λίγκες, όχι σαν κύρια πηγή. Αν δεν βάλεις αυτό το key, ο
  agent απλά παραλείπει αυτές τις 7 λίγκες χωρίς πρόβλημα).

  ⚠️ Πριν το πρώτο run, επιβεβαίωσε τα league IDs (μπορεί να έχουν αλλάξει):
  ```
  curl -H "x-apisports-key: ΤΟ_KEY_ΣΟΥ" \
    "https://v3.football.api-sports.io/leagues?name=Super%20League"
  ```
  και σύγκρινε το `id` που επιστρέφει με αυτά μέσα στο `AF_LEAGUES` dict
  στην αρχή του `value_bet_agent.py`. Αν διαφέρει, άλλαξέ το εκεί.

> ⏱️ Σημείωση: η πληρέστερη ανάλυση σημαίνει περισσότερα requests στο
> football-data.org (standings, ματς έδρας/εκτός, head-to-head ανά ματς).
> Λόγω του free-tier rate limit (10 req/λεπτό), το run μπορεί να πάρει
> 10-20 λεπτά τις μέρες με πολλά ματς — απόλυτα φυσιολογικό, το GitHub
> Actions δεν έχει πρόβλημα με αυτό.

## 3. GitHub repo setup

1. Κάνε push αυτόν τον φάκελο σε ένα **public** repo (το public σου δίνει
   unlimited δωρεάν Actions minutes, όπως και στο trading agent σου).
2. Repo → Settings → Secrets and variables → Actions → **New repository secret**,
   πρόσθεσε:
   - `FOOTBALL_DATA_API_KEY`
   - `ODDS_API_KEY`
   - `API_FOOTBALL_KEY` (προαιρετικό — μόνο αν θες τις 7 επιπλέον λίγκες)
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Το workflow (`.github/workflows/daily_value_bet.yml`) τρέχει αυτόματα
   κάθε μέρα στις 09:00 ώρα Κύπρου. Μπορείς και να το τρέξεις χειροκίνητα
   από το tab **Actions → Daily Value Bet Signal → Run workflow**.

## Πώς δουλεύει το μοντέλο (πλήρης ανάλυση, όχι μόνο τιμή)

Για κάθε ματς, πριν βγει πρόταση, μαζεύονται:

1. **Form έδρας/εκτός ξεχωριστά** — όχι ένας γενικός μέσος όρος. Η γηπεδούχος
   εξετάζεται με βάση το πώς παίζει *σαν γηπεδούχος* τα τελευταία ματς, η
   φιλοξενούμενη με βάση το πώς παίζει *εκτός έδρας*. Αν δεν υπάρχουν αρκετά
   τέτοια ματς (<3), γίνεται blend με το γενικό της form αντί να αγνοηθεί.
2. **Βαθμολογία (points-per-game)** — μικρή διόρθωση (±10% max) πάνω στο
   goals-based μοντέλο, σαν sanity-check.
3. **Head-to-head ιστορικό** — οι τελευταίες αναμετρήσεις μεταξύ των δύο
   ομάδων. Δεν αλλάζει το μοντέλο, αλλά αν έρχεται σε αντίθεση με το pick,
   αυτό υποβαθμίζει τη σιγουριά.
4. **Poisson μοντέλο** πάνω στα παραπάνω → πιθανότητες 1X2 + Over/Under 2.5.
5. **Πολλαπλά bookmakers** (όχι 1) → "δίκαιη" τιμή αγοράς χωρίς vig, ΚΑΙ
   έλεγχος πόσα bookmakers συμφωνούν (λιγότερη αξιοπιστία αν είναι μόνο 1).

**Confidence tiers** — δεν κοιτάνε μόνο πιθανότητα/edge, αλλά και πόσο
"πλήρης" ήταν η ανάλυση πίσω από το pick:

- 🟢 **Υψηλής σιγουρίας**: μοντέλο ≥60%, edge ≥3%, αρκετά δεδομένα, ≥2
  bookmakers συμφωνούν, το H2H δε διαφωνεί
- 🟡 **Μέτριας σιγουρίας**: μοντέλο ≥50%, θετικό edge, τουλάχιστον ένα
  αξιόπιστο σήμα
- 🔴 **Χαμηλής σιγουρίας**: το καλύτερο διαθέσιμο, όχι κάτι που θα έπαιζε
  κανονικά — απλά δεν υπήρχε τίποτα καλύτερο εκείνη τη μέρα

Στέλνει **πάντα 1 pick/μέρα** — ποτέ δε μένεις χωρίς μήνυμα — αλλά το tier
σου λέει πότε να το εμπιστευτείς και πότε καλύτερα να προσπεράσεις τη μέρα.

## Κάλυψη λιγκών

**Μέσω football-data.org** (13): Premier League, La Liga, Serie A,
Bundesliga, Ligue 1, Champions League, Championship, Primeira Liga,
Eredivisie, Brasileirão, World Cup, Euro, Copa Libertadores.

**Μέσω API-Football** (7, προαιρετικό): Ελλάδα (Super League), Αυστρία
(Bundesliga), Ελβετία (Super League), Πολωνία (Ekstraklasa), Τουρκία
(Süper Lig), Σκωτία (Premiership), Βέλγιο (Pro League).

Δεν καλύπτονται (δεν υπάρχει αξιόπιστη δωρεάν πηγή γι' αυτές): Κύπρος,
MLS, J-League, και μικρότερες λίγκες γενικά.

## Επόμενα βήματα (μετά το ποδόσφαιρο)

Όταν επιβεβαιώσουμε ότι το ποδόσφαιρο δουλεύει καλά, προσθέτουμε:
- **Μπάσκετ**: ξεχωριστό μοντέλο (point differential, όχι Poisson goals),
  στατιστικά από balldontlie.io (δωρεάν, μόνο NBA) + αποδόσεις από
  the-odds-api (`basketball_nba`, `basketball_euroleague`).
- **Τένις**: χωρίς αξιόπιστη δωρεάν πηγή rankings/H2H, το pick θα
  βασίζεται κυρίως σε σύγκριση/συμφωνία αποδόσεων μεταξύ bookmakers
  (πιο αδύναμο σήμα από το ποδόσφαιρο — θα το σημειώνει καθαρά).

## Περιορισμοί (διάβασέ τα πριν βασιστείς πάνω τους)

- Το μοντέλο είναι απλό (goals-based Poisson) — δεν βλέπει τραυματισμούς,
  αποβολές, κίνητρο, ή νέα της τελευταίας στιγμής.
- "Edge" εδώ σημαίνει διαφορά μοντέλου vs. αγοράς, ΟΧΙ εγγυημένο κέρδος.
  Ακόμα και σωστά μοντέλα χάνουν συχνά βραχυπρόθεσμα λόγω variance.
- Το matching ομάδων μεταξύ football-data.org και the-odds-api γίνεται με
  βάση το όνομα — σε σπάνιες περιπτώσεις μπορεί να μη βρει αντιστοιχία.
- Στοίχημα = ρίσκο. Χρησιμοποίησε το ως ένα ακόμα data point, όχι σαν
  σίγουρη πρόταση.
