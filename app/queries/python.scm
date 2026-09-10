; Python definitions for app/parsing.py. Every pattern captures @name and @body.
; `async def` is the same function_definition node; decorators live on the parent.

(class_definition
  name: (identifier) @name
  body: (block) @body) @definition.class

(function_definition
  name: (identifier) @name
  body: (block) @body) @definition.function
