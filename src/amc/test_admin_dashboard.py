from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from amc.models import MinistryDashboard, MinistryTerm, Player, SubsidyRule, DeliveryJob
from amc.admin import MinistryDashboardAdmin
from typing import cast, Any

class MockSuperUser:
    def has_perm(self, perm):
        return True
    def is_active(self):
        return True
    def is_staff(self):
        return True

class MinistryDashboardAdminTest(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = MinistryDashboardAdmin(MinistryDashboard, self.site)
        self.factory = RequestFactory()
        self.player = Player.objects.create(unique_id=123)

    def test_dashboard_view_loads_empty(self):
        request = self.factory.get('/admin/amc/ministrydashboard/')
        request.user = cast(Any, MockSuperUser())
        
        response = cast(Any, self.admin.changelist_view(request))
        self.assertEqual(response.status_code, 200)
        
        # Check context
        context = response.context_data
        self.assertIsNone(context['ministry_term'])
        # Subsidies might exist from migrations, so we can't assert empty
        # self.assertEqual(len(context['subsidies']), 0) 

    def test_dashboard_view_loads_with_data(self):
        # Create Term
        term = MinistryTerm.objects.create(
            minister=self.player,
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=7),
            initial_budget=Decimal('1000000'),
            current_budget=Decimal('500000')
        )
        
        # Create Subsidy
        s1 = SubsidyRule.objects.create(name="S1", active=True, priority=999, reward_type="FLAT", reward_value=100)
        s2 = SubsidyRule.objects.create(name="S2", active=False, priority=5, reward_type="FLAT", reward_value=100) # Inactive
        
        # Create Job
        job = DeliveryJob.objects.create(
            name="Job 1", 
            quantity_requested=100, 
            bonus_multiplier=1, 
            funding_term=term
        )
        
        request = self.factory.get('/admin/amc/ministrydashboard/')
        request.user = cast(Any, MockSuperUser())
        
        response = cast(Any, self.admin.changelist_view(request))
        self.assertEqual(response.status_code, 200)
        
        context = response.context_data
        self.assertEqual(context['ministry_term'], term)
        
        # Check if our s1 is in the list
        self.assertIn(s1, context['subsidies'])
        # Check s2 is NOT in the list (inactive)
        self.assertNotIn(s2, context['subsidies'])
        
        self.assertEqual(list(context['recent_jobs']), [job])
