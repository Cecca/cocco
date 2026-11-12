## [Unreleased]

### 🚀 Features

- *(editor)* Reference solutions: graded like a submission, shown on the leaderboard unranked

## [0.1.0] - 2026-08-14

### 🚀 Features

- *(model)* Record the uploaded filename and mark public test cases
- *(web)* Gate the dummy reseed and make the seed usable
- *(repository)* Queries for the challenge page
- *(web)* Submit, leaderboard and sample-download routes
- *(web)* Port the challenge page design
- *(web)* Let course staff edit a challenge
- *(web)* Allow marking test cases as active/retired
- *(web)* Give course staff their own view of a challenge
- *(web)* Let course staff choose what the leaderboard ranks by
- *(web)* Give a submission a page of its own
- *(web)* Sign in with an emailed magic link
- *(web)* Sign in with Google
- Grade with teacher-uploaded evaluator scripts
- *(web)* Create a challenge from the course page
- *(web)* Edit the course page and reorder its challenges
- *(web)* Create a course from the home page
- *(web)* Redesign the courses page and filter it by name
- *(web)* Let students enroll themselves on a course
- *(web)* Make enrollment the gate on a course's contents
- *(web)* Guard enrolment with an optional enrollment key
- *(web)* Make administrators site-wide, not a role on a course
- *(web)* Add an admin page, and a blacklist to go with it
- *(web)* Let course staff manage who is enrolled
- *(web)* Give every account a profile page
- *(web)* Give the challenge page a real breadcrumb trail
- *(web)* Give every page a real breadcrumb trail
- *(web)* Add icon and favicon
- *(api)* Pull and push a challenge as a zip from the command line
- *(build)* Make cocco an installable package with three commands
- *(web)* Archive and delete a course or a challenge
- *(auth)* Sign in as anyone from a box, in development
- *(db)* Migrate the schema with alembic

### 🐛 Bug Fixes

- *(judge)* Don't wrap the queue pull in an outer transaction
- *(repository)* Filter enrollments by user_id, not course_id
- *(test)* Look dave up by the address he actually has
- *(judge)* Resolve languages.toml against the checkout, not the cwd
- *(web)* Stop the navbar mark flickering on every reload
- *(judge)* Compile a submission under the name it was uploaded with

### 📚 Documentation

- Bring CLAUDE.md in line with the current code

### 🚜 Refactor

- *(config)* Read settings from the file, secrets from the environment

### 🧪 Testing

- Repair the repository tests

### ⚙️ Miscellaneous Tasks

- Add Justfile
- Load .env file in Justfile
- Ignore `.scratch` directory
