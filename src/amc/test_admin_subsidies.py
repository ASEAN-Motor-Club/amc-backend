from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from amc.models import SubsidyRule
from amc.admin import SubsidyRuleAdmin
from decimal import Decimal

class MockSuperUser:
    def has_perm(self, perm):
        return True

class SubsidyAdminTest(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = SubsidyRuleAdmin(SubsidyRule, self.site)
        self.factory = RequestFactory()
        
        # Create rules
        self.r1 = SubsidyRule.objects.create(name="R1", priority=1, reward_type="FLAT", reward_value=Decimal("10"))
        self.r2 = SubsidyRule.objects.create(name="R2", priority=2, reward_type="FLAT", reward_value=Decimal("20"))
        self.r3 = SubsidyRule.objects.create(name="R3", priority=3, reward_type="FLAT", reward_value=Decimal("30"))

    def test_reorder_view_updates_priority(self):
        # We want the order to be R1, R2, R3 (top to bottom)
        # So R1 should get highest priority (3), R2 (2), R3 (1)
        # IDs sent: [r1.id, r2.id, r3.id]
        
        request = self.factory.post('/admin/amc/subsidyrule/reorder/', {
            'ids[]': [self.r1.id, self.r2.id, self.r3.id]
        })
        request.user = MockSuperUser() # Not strictly checked in the view currently unless admin view wrap enforces it
        
        response = self.admin.reorder_view(request)
        self.assertEqual(response.status_code, 200)
        
        self.r1.refresh_from_db()
        self.r2.refresh_from_db()
        self.r3.refresh_from_db()
        
        self.assertEqual(self.r1.priority, 3)
        self.assertEqual(self.r2.priority, 2)
        self.assertEqual(self.r3.priority, 1)

    def test_reorder_view_updates_priority_reverse(self):
        # Reverse order: R3, R2, R1
        request = self.factory.post('/admin/amc/subsidyrule/reorder/', {
            'ids[]': [self.r3.id, self.r2.id, self.r1.id]
        })
        
        response = self.admin.reorder_view(request)
        self.assertEqual(response.status_code, 200)
        
        self.r1.refresh_from_db()
        self.r2.refresh_from_db()
        self.r3.refresh_from_db()
        
        self.assertEqual(self.r3.priority, 3)
        self.assertEqual(self.r2.priority, 2)
        self.assertEqual(self.r1.priority, 1)

    def test_reorder_view_no_ids(self):
        request = self.factory.post('/admin/amc/subsidyrule/reorder/', {})
        response = self.admin.reorder_view(request)
        self.assertEqual(response.status_code, 400)

    def test_reorder_view_get_method_not_allowed(self):
        request = self.factory.get('/admin/amc/subsidyrule/reorder/')
        response = self.admin.reorder_view(request)
        self.assertEqual(response.status_code, 405)
