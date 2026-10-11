import unittest
from prompt_manager.core.template_engine import (
    extract_variables,
    hydrate_template,
    get_unfilled_variables,
    check_syntax_errors,
)


class TestTemplateEngine(unittest.TestCase):

    def test_extract_simple_variables(self):
        template = "Hello {{name}}, welcome to {{place}}!"
        specs = extract_variables(template)
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].name, "name")
        self.assertEqual(specs[0].default_value, "")
        self.assertFalse(specs[0].is_multiline)
        self.assertEqual(specs[1].name, "place")

    def test_extract_variables_with_defaults_and_modifiers(self):
        template = (
            "Analyze this {{language:Python}} code:\n"
            "```\n{{code|multiline}}\n```\n"
            "Choose style: {{style:formal|options:casual,formal,concise}}"
        )
        specs = extract_variables(template)
        self.assertEqual(len(specs), 3)

        # language
        self.assertEqual(specs[0].name, "language")
        self.assertEqual(specs[0].default_value, "Python")
        self.assertFalse(specs[0].is_multiline)

        # code
        self.assertEqual(specs[1].name, "code")
        self.assertTrue(specs[1].is_multiline)

        # style
        self.assertEqual(specs[2].name, "style")
        self.assertEqual(specs[2].default_value, "formal")
        self.assertEqual(specs[2].options, ["casual", "formal", "concise"])

    def test_hydrate_template_with_values(self):
        template = "Generate a {{framework:pytest}} test for {{func_name}}."
        values = {"framework": "vitest", "func_name": "computeTotal"}
        result = hydrate_template(template, values)
        self.assertEqual(result, "Generate a vitest test for computeTotal.")

    def test_hydrate_template_fallback_to_defaults(self):
        template = "Language is {{lang:Rust}}."
        values = {}
        result = hydrate_template(template, values, fallback_to_defaults=True)
        self.assertEqual(result, "Language is Rust.")

    def test_unfilled_variables(self):
        template = "Target {{target}} with default {{mode:fast}} and missing {{action}}."
        values = {"target": "production"}
        unfilled = get_unfilled_variables(template, values)
        self.assertEqual(unfilled, ["action"])

    def test_syntax_checker(self):
        good = "Correct {{var}} here."
        bad = "Broken {{unclosed brace here."
        self.assertEqual(len(check_syntax_errors(good)), 0)
        self.assertTrue(len(check_syntax_errors(bad)) > 0)


if __name__ == "__main__":
    unittest.main()
