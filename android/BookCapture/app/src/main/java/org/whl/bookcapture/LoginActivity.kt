package org.whl.bookcapture

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityLoginBinding

/** A Library Tool account; it may be the desktop account or a contributor
 *  account linked to it by the library. The session persists on the device,
 *  so this screen appears once, not per use. */
class LoginActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLoginBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityLoginBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.email.setText(Prefs.email(this))

        binding.signIn.setOnClickListener { submit() }
        binding.continueLocal.setOnClickListener { goToMain(signedIn = false) }
    }

    private fun submit() {
        val email = binding.email.text.toString().trim()
        val password = binding.password.text.toString()
        if (email.isEmpty() || password.isEmpty()) {
            binding.msg.text = getString(R.string.login_missing)
            return
        }
        binding.signIn.isEnabled = false
        binding.continueLocal.isEnabled = false
        binding.msg.text = getString(R.string.login_working)
        lifecycleScope.launch {
            val err = withContext(Dispatchers.IO) {
                Auth.signIn(this@LoginActivity, email, password)
            }
            binding.signIn.isEnabled = true
            binding.continueLocal.isEnabled = true
            if (err == null && Auth.signedIn(this@LoginActivity)) goToMain(signedIn = true)
            else binding.msg.text = err ?: getString(R.string.login_failed)
        }
    }

    /** Lands on Home (the recent-scans list), not the camera. */
    private fun goToMain(signedIn: Boolean) {
        if (signedIn) UploadWorker.enqueue(this)
        // reset the task to a single clean Home
        startActivity(Intent(this, HomeActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
        finish()
    }
}
