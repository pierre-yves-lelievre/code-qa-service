; JavaScript definitions for app/parsing.py, shared by the javascript, typescript and tsx
; grammars (typescript.scm adds the TS-only ones), so it uses only node types all three have: a TS
; class name is a type_identifier, a JS one an identifier, hence `(_)`. Every pattern captures
; @name and @body.

(function_declaration
  name: (identifier) @name
  body: (statement_block) @body) @definition.function

(generator_function_declaration
  name: (identifier) @name
  body: (statement_block) @body) @definition.function

(class_declaration
  name: (_) @name
  body: (class_body) @body) @definition.class

(method_definition
  name: (_) @name
  body: (statement_block) @body) @definition.method

; `const f = () => ...` and `const f = function () {}`: anchored on the declarator, so two
; functions declared in one statement stay two rows.
(variable_declarator
  name: (identifier) @name
  value: [
    (arrow_function body: (_) @body)
    (function_expression body: (statement_block) @body)
  ]) @definition.function
