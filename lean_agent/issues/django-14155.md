repo_id: django/django
repo_path: repos/django_django
ticket_id: django-14155

**Title:** django__django-14155

ResolverMatch.__repr__() doesn't handle functools.partial() nicely.

When a partial function is passed as the view, the __repr__ shows the func argument as functools.partial which isn't very helpful, especially as it doesn't reveal the underlying function or arguments provided.

Because a partial function also has arguments provided up front, we need to handle those specially so that they are accessible in __repr__.

The issue is that when you use functools.partial to create a view, the string representation doesn't show useful information about what the partial wraps or what arguments it has pre-filled. This makes debugging harder since you can't see the actual underlying function or its bound arguments.

A solution would be to unwrap functools.partial objects when initializing the ResolverMatch, so that the underlying function and its arguments are properly exposed in the representation.
