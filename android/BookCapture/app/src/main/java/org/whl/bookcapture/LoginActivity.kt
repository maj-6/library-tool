package org.whl.bookcapture

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabsIntent
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityLoginBinding

/** A Library Tool account; it may be the desktop account or a contributor
 *  account linked to it by the library. The session persists on the device,
 *  so this screen appears once, not per use. Email/password or an OAuth
 *  provider (Google/GitHub) — both land in the same stored session. */
class LoginActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLoginBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityLoginBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.email.setText(Prefs.email(this))

        binding.signIn.setOnClickListener { submit(register = false) }
        binding.register.setOnClickListener { submit(register = true) }
        binding.googleSignIn.setOnClickListener { launchOAuth("google") }
        binding.githubSignIn.setOnClickListener { launchOAuth("github") }

        handleRedirect(intent)   // a cold-start OAuth redirect arrives in the launch intent
    }

    // launchMode=singleTop delivers the redirect here when the activity is warm
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleRedirect(intent)
    }

    private fun launchOAuth(provider: String) {
        binding.msg.text = getString(R.string.login_working)
        val url = Auth.oauthAuthorizeUrl(this, provider)
        CustomTabsIntent.Builder().build().launchUrl(this, Uri.parse(url))
    }

    /** Handle org.whl.bookcapture://auth-callback?code=… coming back from the tab. */
    private fun handleRedirect(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme != "org.whl.bookcapture") return
        setIntent(Intent())                       // consume so a recreate doesn't re-fire it
        val code = data.getQueryParameter("code")
        if (code == null) {
            binding.msg.text = data.getQueryParameter("error_description")
                ?: getString(R.string.login_failed)
            return
        }
        binding.msg.text = getString(R.string.login_working)
        lifecycleScope.launch {
            val err = withContext(Dispatchers.IO) { Auth.completeOAuth(this@LoginActivity, code) }
            if (err == null && Auth.signedIn(this@LoginActivity)) goToMain()
            else binding.msg.text = err ?: getString(R.string.login_failed)
        }
    }

    private fun submit(register: Boolean) {
        val email = binding.email.text.toString().trim()
        val password = binding.password.text.toString()
        if (email.isEmpty() || password.isEmpty()) {
            binding.msg.text = getString(R.string.login_missing)
            return
        }
        binding.signIn.isEnabled = false
        binding.register.isEnabled = false
        binding.msg.text = getString(R.string.login_working)
        lifecycleScope.launch {
            val err = withContext(Dispatchers.IO) {
                if (register) Auth.register(this@LoginActivity, email, password)
                else Auth.signIn(this@LoginActivity, email, password)
            }
            binding.signIn.isEnabled = true
            binding.register.isEnabled = true
            if (err == null && Auth.signedIn(this@LoginActivity)) goToMain()
            else binding.msg.text = err ?: getString(R.string.login_failed)
        }
    }

    /** Shared success path for the password and OAuth flows. Lands on Home (the
     *  recent-scans list), the app's new landing screen, not the camera. */
    private fun goToMain() {
        // the queue may hold captures made while signed out
        UploadWorker.enqueue(this)
        // reset the task to a single clean Home
        startActivity(Intent(this, HomeActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
        finish()
    }
}
