; ─── Functions ───────────────────────────────────────────────────────────────

; Plain function definition
(function_definition
  name: (identifier) @fn.name
  parameters: (parameters) @fn.params
  body: (block . (expression_statement (string) @fn.docstring)?)
) @fn.def

; Async function
(function_definition
  "async" @fn.async
  name: (identifier) @fn.name
) @fn.def

; ─── Classes ──────────────────────────────────────────────────────────────────

(class_definition
  name: (identifier) @class.name
  superclasses: (argument_list)? @class.bases
  body: (block . (expression_statement (string) @class.docstring)?)
) @class.def

; ─── Decorators ───────────────────────────────────────────────────────────────

; Decorator with no arguments: @require_auth
(decorated_definition
  (decorator
    (identifier) @decorator.name) @decorator
  definition: [
    (function_definition name: (identifier) @decorated.name)
    (class_definition    name: (identifier) @decorated.name)
  ]
) @decorated.def

; Decorator with arguments: @rate_limit(max=100)
(decorated_definition
  (decorator
    (call function: (identifier) @decorator.name)) @decorator
  definition: [
    (function_definition name: (identifier) @decorated.name)
    (class_definition    name: (identifier) @decorated.name)
  ]
) @decorated.def

; ─── Imports ─────────────────────────────────────────────────────────────────

; import os, import sys
(import_statement
  name: (dotted_name) @import.module
) @import.stmt

; from os import path
(import_from_statement
  module_name: (dotted_name) @import.from.module
  name: (dotted_name) @import.from.name
) @import.from.stmt

; from os import path, getcwd
(import_from_statement
  module_name: (dotted_name) @import.from.module
  name: (aliased_import
    name: (dotted_name) @import.from.name
    alias: (identifier) @import.from.alias)
) @import.from.aliased.stmt

; from . import utils (relative)
(import_from_statement
  module_name: (relative_import) @import.relative.module
  name: (dotted_name) @import.relative.name
) @import.relative.stmt

; ─── Function calls ───────────────────────────────────────────────────────────

; Simple call: func(args)
(call
  function: (identifier) @call.name
) @call.expr

; Method call: obj.method(args)
(call
  function: (attribute
    object: (identifier) @call.object
    attribute: (identifier) @call.method)
) @call.method.expr

; Dynamic call: getattr(obj, 'method')
(call
  function: (identifier) @call.getattr (#eq? @call.getattr "getattr")
  arguments: (argument_list
    (identifier) @getattr.obj
    [(string) (concatenated_string) (binary_operator)] @getattr.key)
) @call.dynamic

; ─── Module-level exports / __all__ ──────────────────────────────────────────

(expression_statement
  (assignment
    left: (identifier) @export.all (#eq? @export.all "__all__")
    right: (list) @export.names)
) @export.def

; ─── Module-level variable assignments ────────────────────────────────────────

(module
  (expression_statement
    (assignment
      left: (identifier) @var.name
      right: (_) @var.value)
  ) @var.def
)

; ─── Class-level attribute assignments ────────────────────────────────────────
; Captures: class Foo: bar = SomeType(...)
; These define the class API (attributes, descriptors, type annotations)

(class_definition
  name: (identifier) @classattr.class
  body: (block
    (expression_statement
      (assignment
        left: (identifier) @classattr.name
        right: (_) @classattr.value)
    ) @classattr.def
  )
)
