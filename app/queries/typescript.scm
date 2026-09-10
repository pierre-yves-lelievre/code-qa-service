; TypeScript-only definitions for app/parsing.py, appended to javascript.scm for the typescript
; and tsx grammars (the javascript grammar rejects these node types). Every pattern captures @name
; and @body; a type alias has no body, so its value stands in and the signature stops at `=`.

(abstract_class_declaration
  name: (type_identifier) @name
  body: (class_body) @body) @definition.class

(interface_declaration
  name: (type_identifier) @name
  body: (interface_body) @body) @definition.type

(type_alias_declaration
  name: (type_identifier) @name
  value: (_) @body) @definition.type

(enum_declaration
  name: (identifier) @name
  body: (enum_body) @body) @definition.type
