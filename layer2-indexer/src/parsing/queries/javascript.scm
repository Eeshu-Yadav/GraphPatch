; ─── Functions ───────────────────────────────────────────────────────────────

; function foo() {}
(function_declaration
  name: (identifier) @fn.name
  body: (statement_block)
) @fn.def

; async function foo() {}
(function_declaration
  "async" @fn.async
  name: (identifier) @fn.name
) @fn.def

; const foo = function() {}
(variable_declarator
  name: (identifier) @fn.name
  value: (function_expression)
) @fn.expr.def

; const foo = () => {}
(variable_declarator
  name: (identifier) @fn.name
  value: (arrow_function)
) @fn.arrow.def

; const foo = async () => {}
(variable_declarator
  name: (identifier) @fn.name
  value: (arrow_function "async" @fn.async)
) @fn.async.arrow.def

; ─── Classes ─────────────────────────────────────────────────────────────────

; JS uses identifier (not type_identifier) for class names
(class_declaration
  name: (identifier) @class.name
) @class.def

; ─── Methods ─────────────────────────────────────────────────────────────────

(method_definition
  name: (property_identifier) @method.name
) @method.def

; ─── Imports ─────────────────────────────────────────────────────────────────

; import { X, Y } from './module'
(import_statement
  (import_clause
    (named_imports
      (import_specifier name: (identifier) @import.name
                        alias: (identifier)? @import.alias)))
  source: (string) @import.source
) @import.named.stmt

; import DefaultExport from './module'
(import_statement
  (import_clause (identifier) @import.default)
  source: (string) @import.source
) @import.default.stmt

; import * as Namespace from './module'
(import_statement
  (import_clause
    (namespace_import (identifier) @import.namespace))
  source: (string) @import.source
) @import.namespace.stmt

; require('./module') calls
(call_expression
  function: (identifier) @require (#eq? @require "require")
  arguments: (arguments (string) @require.path)
) @require.call

; ─── Exports ─────────────────────────────────────────────────────────────────

; export function foo() {}
(export_statement
  (function_declaration name: (identifier) @export.fn.name)
) @export.fn

; export class Foo {}
(export_statement
  (class_declaration name: (identifier) @export.class.name)
) @export.class

; export const foo = ...
(export_statement
  (lexical_declaration
    (variable_declarator name: (identifier) @export.var.name))
) @export.var

; export { X, Y }
(export_statement
  (export_clause
    (export_specifier name: (identifier) @export.name
                      alias: (identifier)? @export.alias))
) @export.named

; export default
(export_statement "default" @export.default) @export.default.stmt

; ─── Function calls ──────────────────────────────────────────────────────────

; foo(args)
(call_expression
  function: (identifier) @call.name
) @call.expr

; obj.method(args)
(call_expression
  function: (member_expression
    object: (identifier) @call.object
    property: (property_identifier) @call.method)
) @call.method.expr

; new Foo(args) — instantiation
(new_expression
  constructor: (identifier) @new.class
) @new.expr
