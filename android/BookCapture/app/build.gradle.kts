plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from the environment (local shell or CI secrets), so no
// key material ever sits in the repo. Without WHL_KEYSTORE_FILE the release
// build falls back to the debug key: still installable for sideloading, but
// Android treats builds signed with different keys as different authors, so an
// update over an old install needs an uninstall first. Keep one keystore.
val releaseKeystore: String? =
    System.getenv("WHL_KEYSTORE_FILE")?.takeIf { it.isNotBlank() }

// The Supabase project the app signs into. The anon key is public by design
// (the website ships it to every visitor); it is the LOGIN that authorizes
// anything. CI injects these from the repo variables; a blank fallback just
// means Settings must point at a project before first use. Escaped so a stray
// quote/backslash in a var can't break the generated BuildConfig string.
fun env(name: String) = (System.getenv(name)?.trim() ?: "")
    .replace("\\", "\\\\").replace("\"", "\\\"")

android {
    namespace = "org.whl.bookcapture"
    compileSdk = 34

    defaultConfig {
        applicationId = "org.whl.bookcapture"
        minSdk = 26
        targetSdk = 34
        versionCode = 15
        versionName = "0.5.0-alpha.9"
        buildConfigField("String", "SUPABASE_URL", "\"${env("WHL_SUPABASE_URL")}\"")
        buildConfigField("String", "SUPABASE_ANON_KEY", "\"${env("WHL_SUPABASE_ANON_KEY")}\"")
    }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("WHL_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("WHL_KEY_ALIAS") ?: "bookcapture"
                keyPassword = System.getenv("WHL_KEY_PASSWORD")
                    ?: System.getenv("WHL_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (releaseKeystore != null)
                signingConfigs.getByName("release")
            else
                signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.browser:browser:1.8.0")   // Custom Tabs for OAuth sign-in
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.exifinterface:exifinterface:1.3.7")

    val camerax = "1.3.4"
    implementation("androidx.camera:camera-core:$camerax")
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.camera:camera-view:$camerax")

    implementation("androidx.work:work-runtime-ktx:2.9.1")

    // offline keyword spotting ("start" / "photo" / "done" / "cancel")
    implementation("com.alphacephei:vosk-android:0.3.47")
}
