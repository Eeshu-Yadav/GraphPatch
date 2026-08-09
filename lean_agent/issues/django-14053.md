repo_id: django/django
repo_path: repos/django_django
ticket_id: django-14053

**Title:** django__django-14053

HashedFilesMixin's post_process() yields multiple times for the same file

Description

When using ManifestStaticFilesStorage or CachedStaticFilesStorage with collectstatic, the post_process() method yields the same original filename multiple times instead of just once per file.

For example, running collectstatic with the contrib.admin app enabled shows:

Copying '/home/vagrant/python/lib/python2.7/site-packages/django/contrib/admin/static/admin/css/base.css'
Post-processed 'admin/css/base.css' as 'admin/css/base.31652d31b392.css'
Post-processed 'admin/css/base.css' as 'admin/css/base.6b517d0d5813.css'
Post-processed 'admin/css/base.css' as 'admin/css/base.6b517d0d5813.css'

But the expected output should be:

Copying '/home/vagrant/python/lib/python2.7/site-packages/django/contrib/admin/static/admin/css/base.css'
Post-processed 'admin/css/base.css' as 'admin/css/base.6b517d0d5813.css'

This happens because the implementation performs multiple passes to handle nested references between files, but yields results after each pass instead of collecting them and yielding once per original file.

Problems this causes:

1) The statistics shown at the end of collectstatic (e.g. "X files copied, ..., Y post-processed") are incorrect because the number of yields doesn't match the actual number of files processed. There can be more post-processed entries than files that were copied.

2) Subclasses that handle yielded files as they come in perform duplicate work. For example, compression tools end up compressing the same file multiple times, which is expensive and increases deploy times.

3) The duplicate yields occur even for files that don't change during subsequent passes. For example, dashboard.css gets yielded three times with the same final hash.

Steps to reproduce: Run collectstatic with ManifestStaticFilesStorage or CachedStaticFilesStorage and observe the post-processed output messages.
