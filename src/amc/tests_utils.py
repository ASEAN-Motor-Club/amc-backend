from django.test import SimpleTestCase
from amc.utils import generate_verification_code

class UtilsTestCase(SimpleTestCase):
    def test_generate_verification_code(self):
        code = generate_verification_code("test_input")
        self.assertEqual(len(code), 4)
        for char in code:
            self.assertIn(char, "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
            self.assertNotIn(char, "0O1Il")

    def test_determinism(self):
        c1 = generate_verification_code("input1")
        c2 = generate_verification_code("input1")
        self.assertEqual(c1, c2)
        
        c3 = generate_verification_code("input2")
        self.assertNotEqual(c1, c3) # Highly likely to be different
