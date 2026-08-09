repo_id: django/django
repo_path: repos/django_django
ticket_id: django-14011

**Title:** django__django-14011

LiveServerTestCase's ThreadedWSGIServer doesn't close database connections after each thread

Description

In Django 2.2.17, I'm seeing the reappearance of a previous issue where the following error occurs at the conclusion of a test run:

OperationalError: database "test_myapp" is being accessed by other users

This error happens when not all of the database connections are closed. In this case, it occurs when running a single test that is a LiveServerTestCase. The error appears in approximately half of test runs, indicating a race condition.

Steps to Reproduce

1. Create a test class that extends LiveServerTestCase
2. Run the test multiple times
3. Observe that approximately 50% of runs fail with the database access error during test cleanup

Expected Behavior

All database connections should be properly closed after each test thread completes, allowing the test database to be destroyed without errors.

Actual Behavior

Database connections remain open after test threads finish, causing the test database destruction to fail with "database is being accessed by other users" error.

Additional Information

The issue appears to be related to how the server handles threading. When using a non-threaded server implementation instead of the threaded variant, the error no longer occurs. This suggests that the threaded server implementation is not properly closing database connections when threads terminate.

The threading support in LiveServerTestCase was added to improve performance, but this introduced race conditions around database connection cleanup during shutdown. The non-deterministic nature of the failure (occurring in roughly 50% of runs) confirms this is a threading synchronization issue rather than a deterministic bug.
