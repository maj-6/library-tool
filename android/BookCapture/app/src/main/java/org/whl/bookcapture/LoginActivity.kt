package org.whl.bookcapture

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivityLoginBinding

/** The same account as the desktop app; the session persists on the device,
 *  so this screen appears once, not per use. */
class LoginActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLoginBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityLoginBinding.inflate(layoutInflater)
        setContentView(binding.root)
        binding.email.setText(Prefs.email(this))

        binding.signIn.setOnClickListener { submit(register = false) }
        binding.register.setOnClickListener { submit(register = true) }
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
            if (err == null && Auth.signedIn(this@LoginActivity)) {
                // the queue may hold captures made while signed out
                UploadWorker.enqueue(this@LoginActivity)
                // reset the task to a single clean MainActivity, whether we got
                // here from a cold start or a sign-out in Settings
                startActivity(Intent(this@LoginActivity, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
                finish()
            } else {
                binding.msg.text = err ?: getString(R.string.login_failed)
            }
        }
    }
}
