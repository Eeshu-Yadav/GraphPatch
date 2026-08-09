repo_id: django/django
repo_path: repos/django_django
ticket_id: django-14140

**Title:** django__django-14140

Combining Q() objects with boolean expressions crashes.

Description

Currently Q objects with 1 child are treated differently during deconstruct. When a Q object has a single child that is not a subscriptable tuple (like an Exists object), calling deconstruct() raises a TypeError.

Steps to reproduce:
1. Create a Q object with a non-subscriptable child, such as an Exists expression
2. Call deconstruct() on that Q object

Expected behavior:
The deconstruct() method should successfully return the deconstructed representation of the Q object.

Actual behavior:
TypeError: 'Exists' object is not subscriptable

The issue occurs because single-child Q objects are handled specially during deconstruction - they attempt to unpack the child as a tuple with subscript notation, which fails when the child is a boolean expression object rather than a simple key-value tuple.

Example that triggers the error:
```python
from django.contrib.auth import get_user_model
from django.db.models import Exists, Q

Q(Exists(get_user_model().objects.filter(username='jim'))).deconstruct()
```

This works fine with multiple children:
```python
Q(x=1, y=2).deconstruct()  # Returns successfully
```

But fails with a single non-tuple child:
```python
Q(x=1).deconstruct()  # Works - returns kwargs
Q(Exists(...)).deconstruct()  # Crashes - tries to subscript the Exists object
```

The root cause is that the deconstruct method assumes single-child Q objects are always simple key-value tuples and attempts to unpack them accordingly, without checking whether the child is actually subscriptable.
