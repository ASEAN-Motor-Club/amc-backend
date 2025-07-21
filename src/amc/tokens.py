from django.contrib.auth.tokens import PasswordResetTokenGenerator

class AccountActivationTokenGenerator(PasswordResetTokenGenerator):
    """
    Custom token generator for account activation or one-time login.
    It creates a hash using the user's primary key and the current timestamp.
    """
    def _make_hash_value(self, user, timestamp):
        # The key components to create a unique, time-sensitive hash.
        return (
            str(user.pk) + str(timestamp) + str(user.is_active)
        )

# Create an instance of the generator
account_activation_token_generator = AccountActivationTokenGenerator()
