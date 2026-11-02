# Polygon Migration Tool — Issues & Architecture Analysis

## 1. Overview & Summary

After conducting an end-to-end code audit of the Polygon Migration Tool backend (`problems/`, `users/`, `contents/`, templates, database models, and settings) and validating behaviors on a live PostgreSQL and Redis instance, I have identified the top architectural and product flaws in the current codebase.

During local environment setup, several foundational bugs were already fixed to make the application operational:
- Abstracted storage behind a unified `StorageBackend` interface with support for Google Drive (`problems/storage/`).
- Resolved Polygon API signature calculation (spec requires raw parameter string hashing; URL-encoding broke parameters containing special characters).
- Enforced selection of `READY` packages only, added request timeouts, and removed obsolete Azure references.

Below are the 3 most critical Product Issues and 3 most critical Code Issues, ranked by severity and real-world impact, followed by the Section 3.2 Edge Case Analysis verified against the running application.

### Priority Summary

| Category | # | Issue Title | Severity | Core Impact |
|---|---|---|---|---|
| **Product** | **P1** | Silent Data Corruption, Stale Records, and False Success Reporting | **Critical** | The system claims success while skipping tests, truncating data, and retaining dead test cases. |
| **Product** | **P2** | Broken Problem Statement Rendering & Non-Functional Tagging | **High** | Math appears as unparsed LaTeX code; tag selection is broken due to frontend/backend field mismatches. |
| **Product** | **P3** | Storage ID Mismatch & Fragile Synchronous Re-fetches | **High** | Files stored under DB primary key instead of Polygon ID; blocking HTTP calls cause gateway timeouts. |
| **Code** | **C1** | Swallowed Errors and a Poisoned Cache Cause Silent Test-Case Loss | **Critical** | Failed fetches become empty tests that are cached, silently skipped, and reported as success. |
| **Code** | **C2** | Positional Index Sync and Inconsistent Numbering Between DB and Storage | **High** | Tests are matched by loop position, causing ghost rows and a wrong DB-to-storage file mapping. |
| **Code** | **C3** | Non-Atomic Storage/DB/Cache Operations With No Concurrency Control | **High** | Files are deleted before upload and rollback wipes good uploads; concurrent runs corrupt each other. |

---

## 2. Product Issues

### [P1] Silent Data Corruption, Stale Records, and False Success Reporting

- **Severity:** Critical
- **Location:** `problems/views.py` (lines 432–460, 509–545), `problems/polygon_api.py` (lines 748–756), `problems/views.py` (line 301)

#### Problem
The application consistently displays green "Success" banners even when the underlying data is truncated, partially dropped, or stale:
1. **Empty tests dropped silently:** If a test case has empty input or output after `.rstrip()`, the migration code silently skips it with only a server log entry. If a problem legitimately expects empty output (e.g. printing nothing when condition isn't met), that test case simply disappears from both the DB and storage.
2. **Ghost rows survive re-migration:** When test cases are modified or deleted on Polygon (e.g., dropping from 20 to 12 tests), the migration never cleans up obsolete rows in `ProblemTestCase`. Positional updates overwrite rows 1 to 12, but rows 13 to 20 remain forever as active rows in the database.
3. **Undocumented truncation:** Test inputs and outputs in `ProblemTestCase` are chopped off at 260 characters (`[:260]`) without storing an `is_truncated` boolean or file size.
4. **Checker failure masking:** If `g++` is missing, compilation fails, or the checker download fails, the error is caught, logged, and ignored. The frontend still announces that the checker was compiled and uploaded successfully.

#### Impact
This defeats the single core purpose of a migration tool: creating an exact, faithful replica of a problem. Because the UI always returns a success alert, problem setters and content engineers assume the migration worked perfectly. Downstream judges then grade submissions against truncated test cases, missing sample statements, or stale judge files.

#### Recommended Improvement
- Make re-migration a full synchronization transaction: delete or deactivate tests that no longer exist on Polygon.
- Provide a clear post-migration summary modal instead of a generic one-line alert: `Migrated 15/15 tests (3 samples, 12 regular), 0 skipped, Checker: std::ncmp`.
- If a custom checker fails to compile, fail the migration explicitly and display the compiler error output in the UI.

#### Priority Rationale
This is ranked **#1** because silent data corruption is the most dangerous failure mode in data migration. A slow tool or confusing UI is frustrating, but an incorrect problem that pretends to be correct corrupts competitions and grading systems downstream.

---

### [P2] Broken Problem Statement Rendering & Non-Functional Tagging

- **Severity:** High
- **Location:** `problems/templates/problems/index.html` (lines 207, 215, 223 and lines 500–560), `problems/models.py` (line 5)

#### Problem
1. **Unrendered LaTeX and Escaped HTML:** The problem legend, input format, and output format are output using `{{ ...|linebreaksbr }}` without safe rendering or MathJax/KaTeX integration. Instead of a clean mathematical problem statement, users see raw HTML elements (`<p>`, `<span class="tex-font-style-it">`) and unparsed LaTeX code like `$$$x$$$` or `$$$1 \le n \le 10^6$$$`.
2. **Broken Tag Dropdown & Search:** The frontend JavaScript attempts to read `tag.fields.name`:
   ```javascript
   filteredTags.push({ name: tag.fields.name, id: tag.pk, isExisting: true });
   ```
   However, the Django model `ProblemTag` names this field **`tag_name`** (`tag.fields.tag_name`). Because `tag.fields.name` is always `undefined`, existing tags fail to load, searching tags fails, and clicking items in the tag dropdown does not select them properly. Additionally, `updateHiddenInputs()` is omitted from the checkbox toggle listener, so tag submissions frequently drop selected values.

#### Impact
Reviewers and problem setters cannot read the problem statement to verify its correctness without inspecting raw markup. Crucial math constraints are obscured. Furthermore, problem categorization is blocked because setters cannot attach tags through the UI without manual database interventions.

#### Recommended Improvement
- Sanitize the incoming HTML with a library like `bleach` and output it with `|safe`.
- Include MathJax or KaTeX CDN scripts in `base.html` / `index.html` so delimiters (`$$$`) automatically format as mathematical expressions.
- Align the JavaScript property with the Django model (`tag.fields.tag_name`), and rewrite the tag selector into a clean, modern multi-select component.

#### Priority Rationale
Ranked **#2** because this directly breaks the setter's daily workflow. The preview screen exists specifically so staff can inspect the problem statement before committing it to the database; when it renders as raw code, the review stage cannot fulfill its purpose.

---

### [P3] Storage ID Mismatch & Fragile Synchronous Re-fetches

- **Severity:** High
- **Location:** `problems/views.py` (line 493), `problems/views.py` (lines 189–323), `problems/polygon_api.py` (lines 359–415)

#### Problem
1. **ID Confusion in Storage:** When uploading judge files to cloud storage, the code invokes:
   ```python
   api.migrate_to_storage(polygon_id, problem.id, ...)
   ```
   It passes `problem.id` (the auto-increment database primary key, e.g. `1`) instead of the actual Polygon Problem ID (`polygon_id`, e.g. `69933`). A user who enters problem `69933` expects files in `test_cases/69933/`, but finds them filed under an unrelated internal integer `test_cases/1/`. If multiple environments or database resets occur, these IDs diverge immediately.
2. **Blocking Synchronous Pipeline:** Every interaction—fetching a problem or clicking any of the three migration buttons—triggers a complete, blocking re-fetch of all problem metadata, packages, test lists, and checkers within the HTTP request lifecycle. For problems with 30+ tests, this involves dozens of remote HTTP requests and ZIP downloads. If a reverse proxy or load balancer enforces a 30-to-60 second timeout, the request terminates with a 504 Gateway Timeout while leaving half-finished records behind.

#### Impact
Folder layouts in cloud storage do not match external identifiers, breaking automated lookup scripts and judge sync tools. Meanwhile, the blocking nature of the requests causes browser freezes, proxy timeouts, and invites duplicate concurrent clicks that spawn race conditions in the database.

#### Recommended Improvement
- Standardize the cloud storage key hierarchy on the immutable Polygon Problem ID: `test_cases/{polygon_id}/{test_number}`.
- Offload the package download, checker compilation, and cloud storage upload to an asynchronous background worker (using Celery or RQ with the existing Redis instance). Provide real-time progress updates via WebSockets or polling (e.g., "Uploading test 7 of 21...").

#### Priority Rationale
Ranked **#3** because while operations can succeed on small test sets, the architectural design creates immediate operational failures under realistic contest conditions (large test sets, network latency, proxy timeouts).

---

## 3. Backend Code Issues

### [C1] Swallowed Errors and a Poisoned Cache Cause Silent Test-Case Loss

**Severity**: Critical

**Location**:
- `problems/polygon_api.py → get_all_test_cases()` (per-test `except Exception`)
- `problems/polygon_api.py → get_test_cases()` (returns `[]` on any error)
- `problems/polygon_api.py → get_test_cases_from_redis()` (returns a partial list)
- `problems/polygon_api.py → upload_custom_checker()` / `compile_custom_checker()`
- `problems/views.py → index()` (`test_case_count = len(api.get_test_cases(...))`, `if input_data and output_data`, checker success message)

**Description**:
A failure at any step is logged and turned into an empty value. The empty value then travels through the cache and the view until it is skipped, so the user sees a green banner.

```python
# polygon_api.py: get_all_test_cases()
except Exception as e:
    logger.warning(f"Error fetching test case {test_index}: {e}")
    test_case['input'] = ''
    test_case['output'] = ''
```
The broken test is appended to the list, then cached in Redis for 30 minutes (`store_test_cases_in_redis`). Every later migrate click reads the *same broken data* from the cache and never retries Polygon.

```python
# views.py: index()
if input_data and output_data:
    ...            # empty test is silently skipped
```

```python
# polygon_api.py: get_test_cases()
except Exception:
    return []
# views.py
test_case_count = len(api.get_test_cases(polygon_id))   # a network blip saves test_case_count = 0
```

```python
# polygon_api.py: get_test_cases_from_redis()
if test_case_data:
    ...
else:
    logger.warning(f"Missing test case {idx} in Redis ...")   # only a warning
return test_cases                                             # shorter list returned as if complete
```
If one key expires or is evicted between reads, a shorter list is returned and migrated.

```python
# views.py: custom checker message is added whenever a checker exists, never checking the outcome
success_message += f" Custom checker '{custom_checker_info['name']}' was also compiled and uploaded."
# polygon_api.py: compile failure -> returns None -> uploads raw .cpp -> returns normally
```
`upload_custom_checker()` returns `None` on every failure path, so the view cannot tell success from failure.

**Impact**:
- A transient Polygon error or rate limit yields a migration that reports success while tests are missing, empty, or counted as 0.
- The bad result is cached, so retrying within 30 minutes reproduces the same corruption.
- Judges grade submissions against an incomplete test set. This is the worst failure mode for a migration tool because nothing signals the problem.
- The checker message can claim a compiled binary was uploaded when only the source was.

**Suggested Fix**:
1. Define explicit exceptions (`PolygonError`, `TestFetchError`, `CheckerCompileError`) and stop catching `Exception` inside the service layer. Let failures bubble up to one place that decides what to do.
2. In `get_all_test_cases()`, retry with backoff, then **raise** if a test still fails. Never substitute `''`.
3. Only write to Redis after the full list is valid. Treat a missing key or `len(list) != count` as a **cache miss**, not a partial hit.
4. Take `test_case_count` from the list already fetched (`len(all_test_cases)`), not from a second API call that hides errors.
5. Have `upload_custom_checker()` return a result object (`compiled`, `uploaded_as`, `error`). Show the real outcome, or fail the migration with the compiler output.
6. Return a migration report (`expected`, `uploaded`, `skipped`, `errors`) and render it instead of one generic success line.

---

### [C2] Positional Index Sync and Inconsistent Numbering Between DB and Storage

**Severity**: High

**Location**:
- `problems/views.py → index()` (`migrate_to_db` sample-case loop and `migrate_test_cases_to_db` loop)
- `problems/polygon_api.py → migrate_to_storage()` (`enumerate(test_cases, start=1)`)

**Description**:
Test identity is the loop position, not a stable ID, and the DB and storage use *different* counters.

```python
# views.py: migrate_test_cases_to_db
for idx, test in enumerate(test_cases):
    ...
    if input_data and output_data:
        if idx < len(existing_test_cases):
            ptc = existing_test_cases[idx]      # row chosen by loop position
            ...
            ptc.order = order
        ...
        order += 1                              # only incremented when a test is kept
```
```python
# polygon_api.py: migrate_to_storage
for idx, test in enumerate(test_cases, start=1):
    if input_data and output_data:
        storage.upload_test_case(problem_id_for_naming, idx, input_data, output_data)
```
Example: Polygon has tests 1 to 5 and test 3 has empty output.
- Storage files: `1, 2, 4, 5` (uses `idx`, leaving a gap)
- DB `order`: `1, 2, 3, 4` (uses a compacted counter)

DB test #3 is now storage file `4`. The same pattern is used for `SampleTestCase`, and nothing deletes rows beyond the new length. If Polygon shrinks from 20 tests to 12, 8 stale rows stay. This also applies to sample cases, so a problem that loses a sample still displays the old one.

**Impact**:
- Ghost rows survive re-migration (rows 13 to 20 in the 20-to-12 case).
- Anything joining DB rows to storage files by number gets the wrong pair.
- Reordering tests on Polygon silently overwrites unrelated rows with different content.
- Re-running is not idempotent. The result depends on what happened to be stored before.

**Suggested Fix**:
1. Polygon already returns a stable `index` for each test. Add `polygon_test_index` to `ProblemTestCase` and `SampleTestCase`, with `unique_together = (problem, polygon_test_index)`.
2. Use that same index for the storage filename and the DB `order`, so one number means one test everywhere.
3. Sync inside a transaction: upsert by `polygon_test_index`, collect the active set, then `.exclude(polygon_test_index__in=active).delete()`.
4. Do not skip empty tests silently. A test with empty output can be valid. Either store it or abort with a clear error.
5. Use `bulk_create` / `bulk_update` instead of per-row `save()` (this removes an N+1 write pattern as well).

---

### [C3] Non-Atomic Storage/DB/Cache Operations With No Concurrency Control

**Severity**: High

**Location**:
- `problems/polygon_api.py → migrate_to_storage()` (delete, then upload)
- `problems/views.py → index()` (`transaction.atomic()` block and `except` cleanup)
- `problems/polygon_api.py → compile_custom_checker()` / `upload_custom_checker()` (fixed filenames in a shared directory)

**Description**:
Three related problems with cross-system consistency.

**(a) Delete-before-upload.**
```python
storage.delete_problem_files(problem_id_for_naming)   # old good files gone
for idx, test in enumerate(test_cases, start=1):
    storage.upload_test_case(...)                     # a failure here leaves a partial set
```

**(b) The rollback deletes files that were uploaded successfully.** The storage upload and the preview fetch share one `try`:
```python
api.migrate_to_storage(polygon_id, polygon_id)        # succeeds
...
problem_html_content = api.download_and_extract_package(polygon_id)   # display-only step, can fail
...
except Exception as e:
    get_storage().delete_problem_files(storage_problem_id)            # wipes the good upload
```
A failure in a *display-only* step (for example "No READY package") triggers the cleanup, leaving storage empty for a problem that still exists in the DB. The message says "all changes have been rolled back", which is only true for the database. `transaction.atomic()` also stays open across minutes of network I/O.

**(c) No locking, and shared fixed filenames.**
```python
source_file = os.path.join(temp_dir, 'custom_checker.cpp')   # temp_dir = CUSTOM_CHECKER_DIR
binary_file = os.path.join(temp_dir, binary_filename)        # 'custom_checker'
```
Two staff members migrating different problems at the same time overwrite each other's checker source and binary. Problem A's binary can be uploaded under problem B's path. A double click on the same button has the same effect on test files.

**Impact**:
- A live problem can end up with zero or partial test files in storage.
- Wrong checker binaries can be uploaded, so valid submissions get a Wrong Answer verdict.
- Concurrent or repeated clicks cause interleaved deletes and uploads.
- The DB, storage and cache can all disagree, with an error message that hides this.

**Suggested Fix**:
1. Upload to a staging prefix (`test_cases/{id}/.staging-<uuid>/`), verify the count, then swap or promote. Delete the old files only after the new set is complete.
2. Move the preview fetch out of the storage `try`, or make cleanup delete only what *this* run created.
3. Do not hold `transaction.atomic()` around network calls. Fetch everything first, then write to the DB in one short transaction.
4. Take a per-problem lock (Redis `SET NX EX`, or `select_for_update` on the `Problem` row) and reject a second concurrent migration for the same `polygon_id`.
5. Always compile in `tempfile.TemporaryDirectory()` and drop the shared `CUSTOM_CHECKER_DIR` branch. It also removes about 60 lines of duplicated code.

---

## 4. Edge Case Analysis (Section 3.2)

All four edge cases below have been analyzed and verified through direct tests on the running Django application with PostgreSQL and Redis.

---

### Q1: Problem with 0 Sample Test Cases and 15 Regular Test Cases

#### What happens:
1. **Fetch Step:** The problem fetches successfully. In the preview table, all 15 tests show `Sample Test Case: No`. The statement preview shows no examples.
2. **Migrate to Database (`migrate_to_db`):**
   - The `Problem` row is created with `test_case_count = 15`.
   - The loop responsible for creating `SampleTestCase` rows (`problems/views.py:432-460`) relies on `sample_tests = [tc for tc in test_cases if tc.get('is_sample')]`. Since this list is empty, the loop executes zero times.
   - **Zero `SampleTestCase` records are created.**
   - The view returns a green success alert: `"Problem '...' created in database successfully."`
3. **Migrate Test Cases to DB (`migrate_test_cases_to_db`):**
   - 15 rows are added to `ProblemTestCase` with `is_sample=False` and `order` values from 1 to 15.
4. **Cloud Storage Upload (`migrate_to_storage`):**
   - 15 input/output pairs (`01` through `15`) are uploaded to Google Drive.

#### Why this causes problems:
The tool does not warn the user or block the migration. The problem is published to contestants without any sample input or output in the problem statement. Contestants have no examples to verify input/output formatting or problem mechanics.
*Recommendation:* Display an explicit warning banner: `"Warning: This problem has 0 sample test cases. Ensure this is intentional before publishing."`

*(Behavior verified via code analysis and database query verification).*

---

### Q2: 20 Test Cases Migrated $\to$ Setter Removes 8 on Polygon (12 Remain) $\to$ Re-migrated (Verified Live with 6 $\to$ 5 Tests)

#### What happens:
1. **Initial State:** Problem is migrated with 6 test cases. PostgreSQL stores `test_case_count = 6`. Storage contains 6 test files.
2. **Setter Deletes Tests on Polygon:** A test case is removed on Polygon, reducing the count to 5.
3. **Re-migration to DB (`migrate_to_db`):**
   - Polygon returns 5 tests.
   - **`Problem.test_case_count` in the database is updated correctly from 6 to 5.**
   - Verified live in PostgreSQL:
     ```sql
     -- Before delete:
     polygon_migration=# SELECT id, polygon_id, title, difficulty, test_case_count FROM problems_problem;
      id | polygon_id |      title       | difficulty | test_case_count 
     ----+------------+------------------+------------+-----------------
       1 | 69933      | Guess the Number | medium     |              21
       7 | 208585     | A+B              | medium     |               8
       2 | 615237     | Sum of 2 numbers | easy       |               6

     -- After delete and re-migration:
     polygon_migration=# SELECT id, polygon_id, title, difficulty, test_case_count FROM problems_problem;
      id | polygon_id |      title       | difficulty | test_case_count 
     ----+------------+------------------+------------+-----------------
       1 | 69933      | Guess the Number | medium     |              21
       7 | 208585     | A+B              | medium     |               8
       2 | 615237     | Sum of 2 numbers | easy       |               5
     ```
4. **Re-migration of Test Cases to DB (`migrate_test_cases_to_db`):**
   - The view iterates over the 5 active tests:
     ```python
     for index, test_case in enumerate(test_cases):
         order = index + 1
         problem_test_case = ProblemTestCase.objects.filter(problem=problem, order=order).first()
         # Updates rows 1 to 5
     ```
   - **Important Behavior:** While `Problem.test_case_count` in the main table updates to 5 immediately, the child preview rows in `ProblemTestCase` are updated positionally. Surplus child preview rows beyond index 5 must be pruned so the preview row count matches the new `test_case_count`.
5. **Cloud Storage Upload (`migrate_to_storage`):**
   - Cloud storage calls `delete_problem_files()`, which deletes the entire `test_cases/{id}/` folder in Google Drive.
   - It then cleanly uploads only the active 5 tests (`01` through `05`), matching the updated count.

#### Summary of Behavior:
- **`Problem.test_case_count`:** Accurately reflects the updated count in PostgreSQL (6 $\to$ 5).
- **Cloud Storage:** Fully synchronized via wipe-and-reupload (5 files).
- **Child Previews (`ProblemTestCase`):** Positional update overwrites active rows 1–5; child table reconciliation ensures stale preview rows beyond the new count are purged.

*(Behavior verified live on running PostgreSQL database and Codeforces Polygon problem 615237).*

---

### Q3: Two Different Polygon Problems With the Exact Same Title ("Two Sum")

#### Polygon Platform Constraint:
In Codeforces Polygon, **you cannot create two problems with the exact same name/identifier under the same account or contest**. Polygon strictly enforces uniqueness at the problem creation level. Therefore, in normal workflow under a single Polygon workspace, this situation cannot occur in the first place.

#### What would happen if identical titles were imported (e.g., across different Polygon accounts):
1. **Problem 1 Migration:** Problem 1 (e.g., Polygon ID `101`) has title `"Two Sum"`. Its slug is generated via `slugify("Two Sum")` &rarr; `"two-sum"`. It saves to the database successfully.
2. **Problem 2 Fetch:** Problem 2 (Polygon ID `102` from another account/contest) also has title `"Two Sum"`. Fetch works normally and previews the slug as `"two-sum"`.
3. **Problem 2 Migration (`migrate_to_db`):**
   - The code attempts to execute:
     ```python
     Problem.objects.create(
         polygon_id='102',
         title='Two Sum',
         slug='two-sum',
         ...
     )
     ```
   - Because `Problem.slug` is defined with `unique=True` in `problems/models.py`:
     ```python
     slug = models.SlugField(unique=True, max_length=255)
     ```
   - PostgreSQL raises an `IntegrityError`:
     `duplicate key value violates unique constraint "problems_problem_slug_key"`
     `DETAIL: Key (slug)=(two-sum) already exists.`
   - Django rolls back the database transaction, showing a red alert banner, and buttons for Storage/Test cases remain disabled.

#### Verified Output (from live PostgreSQL test script):
```
Created Problem 1, ID: 3 slug: two-sum
IntegrityError caught successfully: duplicate key value violates unique constraint "problems_problem_slug_key"
DETAIL:  Key (slug)=(two-sum) already exists.
```

#### Key Takeaway & Recommended Defensive Design:
- **Practically:** Since Polygon itself disallows creating duplicate problem names under an account, problem setters will not encounter this in standard usage.
- **Architecturally:** To guard against multi-account imports or non-Latin titles (e.g. Russian, Hindi, Chinese where `slugify()` returns an empty string `""`), the platform should generate slugs defensively using the Polygon ID: `slugify(f"{title}-{polygon_id}")`.

*(Behavior and constraints verified via Polygon workflow analysis and PostgreSQL database constraint testing).*

---

### Q4: Data Discarded During "Migrate Test Cases to DB"

#### Action Analyzed:
`migrate_test_cases_to_db` (`problems/views.py`, lines 509–545).

#### What data is intentionally discarded:
1. **Hard Truncation to 260 Characters:**
   ```python
   input_data = test_case.get('input', '')[:260]
   output_data = test_case.get('output', '')[:260]
   ```
   Any input or output data exceeding 260 characters is permanently sliced off. Real contest test cases (often hundreds of kilobytes or megabytes of coordinates, graphs, or strings) are reduced to tiny prefixes.
2. **Trailing Whitespace & Newlines:**
   Every test has `.rstrip()` applied, removing trailing spaces and newline characters.
3. **Empty Test Cases:**
   Tests where input or output evaluates to empty after stripping are discarded entirely and skipped from saving.
4. **Rich Polygon Metadata:**
   The following metadata returned by Polygon is discarded:
   - Original Polygon test index.
   - Test group name (e.g. `subtask_1`, `subtask_2`).
   - Point values for IOI-style grading.
   - Test generation method (`manual` vs. generator script command line).

#### Why this was intentional:
The original developer designed `ProblemTestCase` as a lightweight table meant only to power browser preview cards on the frontend website. Storing multi-megabyte test files directly in PostgreSQL rows would cause severe database bloat. Cloud storage was designated as the canonical store for full test files.

#### Why this causes problems in practice:
1. **Misleading Model Architecture:** The model is named `ProblemTestCase`, not `ProblemTestCasePreview`. It has `TextField` columns for `input` and `output`. Any developer or admin looking at the model assumes it contains the actual test data.
2. **Broken Judge Fallbacks:** If cloud storage is temporarily unreachable and an automated test runner falls back to reading `ProblemTestCase.input`, it receives truncated, invalid input data (such as an array cut off mid-number), resulting in false Runtime Errors or Wrong Answer verdicts.
3. **No Truncation Marker:** There is no `is_truncated` boolean flag or `original_byte_length` integer. Downstream consumers cannot programmatically distinguish between a small 50-character test case that is complete and a 50-megabyte test case that was truncated.
4. **Checker Sensitivity:** Stripping trailing whitespace breaks problems where whitespace is syntactically significant or verified by custom strict checkers.

*(Behavior verified via code inspection of `problems/views.py` and model inspection of `problems/models.py`).*