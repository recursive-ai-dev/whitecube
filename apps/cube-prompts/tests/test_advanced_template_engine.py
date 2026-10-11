"""Advanced unit tests for the prompt templating engine."""

import unittest
from prompt_manager.core.template_engine import (
    extract_variables,
    hydrate_template,
    get_unfilled_variables,
    check_syntax_errors,
)


class TestAdvancedTemplateEngine(unittest.TestCase):

    def test_combined_modifiers(self):
        template = "{{code:print('hello')|multiline;options:opt1,opt2}}"
        specs = extract_variables(template)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].name, "code")
        self.assertEqual(specs[0].default_value, "print('hello')")
        self.assertTrue(specs[0].is_multiline)
        self.assertEqual(specs[0].options, ["opt1", "opt2"])

    def test_variable_merging_across_occurrences(self):
        template = "Start with {{var:default_val}} and later specify {{var|multiline}}"
        specs = extract_variables(template)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].name, "var")
        self.assertEqual(specs[0].default_value, "default_val")
        self.assertTrue(specs[0].is_multiline)

    def test_preserve_unfilled(self):
        template = "Hello {{name}}, code is {{code}} and default is {{mode:fast}}."
        result = hydrate_template(template, {"name": "Alice"}, fallback_to_defaults=False, preserve_unfilled=True)
        self.assertEqual(result, "Hello Alice, code is {{code}} and default is {{mode:fast}}.")

    def test_hydrate_empty_template(self):
        self.assertEqual(hydrate_template("", {}), "")

    def test_extract_variables_empty_template(self):
        self.assertEqual(extract_variables(""), [])

    def test_syntax_errors_nested_braces(self):
        template = "Invalid {{nested {{brace}}}}"
        errors = check_syntax_errors(template)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("Nested" in e for e in errors))

    def test_syntax_errors_unmatched_braces(self):
        template = "Unmatched {{var and another {{extra"
        errors = check_syntax_errors(template)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("Unmatched" in e for e in errors))

    def test_syntax_errors_inverted_braces(self):
        template = "}} Inverted {{"
        errors = check_syntax_errors(template)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any("closing" in e.lower() for e in errors))


if __name__ == "__main__":
    unittest.main()
